"""Lectura de sensores BLE: banda de FC (180D/2A37) y rodillo (1818/2A63 + 1826/2ACF).

Incluye simulador y modo probe para volcar la GATT real de un dispositivo.
Compatible macOS (CoreBluetooth) y Linux (BlueZ) via bleak.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
import weakref
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner

# UUIDs SIG estandar (forma corta para busqueda tolerante)
HR_CHAR_S = "2a37"          # Heart Rate Measurement (servicio 180D)
BAT_CHAR_S = "2a19"         # Battery Level (180F)
CP_CHAR_S = "2a63"          # Cycling Power Measurement (1818)
CSC_CHAR_S = "2a5b"         # Cycling Speed and Cadence Data (1816)
FTMS_BIKE_CHAR_S = "2acf"   # Indoor Bike Data (1826)

HR_NAME_HINTS = ("rhythm", "r+", "scosche", "polar", "hrm", "heart", "h10", "h9",
                 "coospo", "mova", "spovan", "decathlon", "kenafit", "wahoo",
                 "tickr", "orion", "band", "cardio")
TRAINER_NAME_HINTS = ("tacx", "flux", "neo", "genio", "techno", "trainer",
                      "bikescope", "zwift", "bryton", "jetblack", "kinetic",
                      "eliter", "kickscape", "stages")


# ---------------------------------------------------------------- parseo ---
def parse_hr_measurement(data: bytearray) -> tuple[int, bool | None]:
    """(fc, contacto) desde la caracteristica 2A37."""
    flags = data[0]
    if flags & 0x01:
        hr = int.from_bytes(data[1:3], "little") & 0x0FFF
    else:
        hr = data[1]
    contact: bool | None = None
    if flags & 0x02:
        contact = bool(flags & 0x04)
    return hr, contact


def parse_cycling_power(data: bytearray):
    """Desde 2A63: (watts, (revs, evtime_1024), flags). Cadencia se deriva fuera.

    Tolerante: algunos Tacx (Satori) envian el bloque de ciguenal aunque los
    flags no declaren bit1; en ese caso se extrae igual si el tamano lo permite.
    """
    d = bytearray(data)
    flags = int.from_bytes(d[0:2], "little")
    pos = 2
    watts = None
    if pos + 2 <= len(d):
        raw = int.from_bytes(d[pos:pos + 2], "little")
        if flags & 0x0001:                 # potencia acumulada (W*s): usar igual
            watts = raw
        else:                              # instantanea sint16
            watts = raw - 65536 if raw > 32767 else raw
        pos += 2
    crank = None
    if flags & 0x0002 and pos + 6 <= len(d):
        revs = int.from_bytes(d[pos:pos + 4], "little")
        evtime = int.from_bytes(d[pos + 4:pos + 6], "little")   # 1/1024 s
        crank = (revs, evtime)
    elif len(d) >= 14:
        # Tacx Satori: dentro de 2A63 trae un payload estilo CSC:
        # [wheelRevs u32][wheelTime u16][crankRevs u16][crankTime u16]
        # -> las revoluciones de BIELA estan en los u16 de offset 10..13
        revs = int.from_bytes(d[10:12], "little")
        evtime = int.from_bytes(d[12:14], "little")
        crank = (revs, evtime)
    return watts, crank, flags


def cadence_from_crank(prev, cur, rev_mod: int = 1 << 32) -> float | None:
    """rpm desde deltas de revoluciones y tiempo de evento (1/1024 s).

    `rev_mod`: modulo del contador de revoluciones (2**32 estandar CP, 65536
    para el formato tipo-CSC del Tacx Satori).
    """
    if not prev or not cur:
        return None
    d_rev = (cur[0] - prev[0]) % rev_mod
    d_t = ((cur[1] - prev[1]) % 65536) / 1024.0
    if d_rev == 0 or d_rev > 20 or not (0.05 < d_t < 10.0):
        return None
    return d_rev / d_t * 60.0


def parse_csc(data: bytearray):
    """Cycling Speed & Cadence (2A5B). Devuelve dict con lo que se pueda.

    Orden estandar: time16 + revs32 por rueda/biela (13 bytes con flags 0x03).
    Tacx Satori: revs32 + time16 y tramos recortados (11 bytes) -> probar ambos.
    """
    d = bytearray(data)
    if len(d) < 3:
        return {}
    flags = d[0]
    pos = 1
    out = {}

    # Tacx Satori: payload recortado de 11 bytes con orden count-first:
    # [wheelRevs u32][wheelTime u16][crankRevs u16][crankTime u16]
    if len(d) == 11 and (flags & 0x03) == 0x03:
        return {"wheel_revs": int.from_bytes(d[1:5], "little"),
                "wheel_time": int.from_bytes(d[5:7], "little"),
                "crank_revs": int.from_bytes(d[7:9], "little"),
                "crank_time": int.from_bytes(d[9:11], "little")}

    def _try(revs_first: bool):
        nonlocal pos
        p = 1
        vals = {}
        try:
            if flags & 0x01:
                if revs_first:
                    vals["wheel_revs"] = int.from_bytes(d[p:p + 4], "little"); p += 4
                    vals["wheel_time"] = int.from_bytes(d[p:p + 2], "little"); p += 2
                else:
                    vals["wheel_time"] = int.from_bytes(d[p:p + 2], "little"); p += 2
                    vals["wheel_revs"] = int.from_bytes(d[p:p + 4], "little"); p += 4
            if flags & 0x02:
                if revs_first:
                    vals["crank_revs"] = int.from_bytes(d[p:p + 4], "little"); p += 4
                    vals["crank_time"] = int.from_bytes(d[p:p + 2], "little"); p += 2
                else:
                    vals["crank_time"] = int.from_bytes(d[p:p + 2], "little"); p += 2
                    vals["crank_revs"] = int.from_bytes(d[p:p + 4], "little"); p += 4
        except IndexError:
            return None
        return vals

    exact = len(d) == (1 + (6 if flags & 1 else 0) + (6 if flags & 2 else 0))
    attempts = ([False] if exact else [True, False])
    for revs_first in attempts:
        vals = _try(revs_first)
        if vals and vals.get("wheel_revs", 0) < (1 << 28):
            return vals
    return {}


def parse_indoor_bike(data: bytearray) -> dict:
    """Desde 2ACF (Indoor Bike Data): speed(km/h), cadence(rpm), dist(m), kcal, hr."""
    d = bytearray(data)
    flags = int.from_bytes(d[0:2], "little")
    pos = 2
    out = {}

    def _u16():
        nonlocal pos
        v = int.from_bytes(d[pos:pos + 2], "little"); pos += 2
        return v

    def _u32():
        nonlocal pos
        v = int.from_bytes(d[pos:pos + 4], "little"); pos += 4
        return v

    try:
        if flags & 0x0002:                          # instantaneous speed, 0.01 km/h
            out["speed_kmh"] = _u16() * 0.01
        if flags & 0x0004:                          # instantaneous cadence, 0.5 rpm
            out["cadence"] = _u16() * 0.5
        if flags & 0x0008:                          # speed+cadence combinados (uint32)
            out["speed_kmh"] = _u16() * 0.01
            out["cadence"] = _u16() * 0.5
        if flags & 0x0010:                          # distancia acumulada, m
            out["dist_m"] = _u32()
        if flags & 0x0020:                          # energia acumulada, kcal
            out["energy_kcal"] = _u16()
        if flags & 0x0040:                          # HR del rodillo (poco comun)
            out["hr"] = d[pos]; pos += 1
    except IndexError:
        pass
    out["_flags"] = flags
    return out


# ---------------------------------------------------------------- escaneo --
@dataclass(frozen=True)
class Found:
    """Dispositivo BLE reconocido en un escaneo (banda de FC o rodillo)."""

    name: str                 # "" si no anuncia nombre
    address: str              # MAC (BlueZ) o UUID de CoreBluetooth
    rssi: int                 # dBm (mas cerca = menos negativo)
    kind: str                 # "hr" | "trainer"
    caps: tuple = ()          # servicios anunciados: "FC", "POT", "FTMS", "CSC"


_SERVICE_CAPS = (("180d", "FC"), ("1818", "POT"), ("1826", "FTMS"), ("1816", "CSC"))
SCAN_TTL = 5.0                # un escaneo reciente se reutiliza (varios consumidores a la vez)
_SCAN_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_SCAN_CACHE: dict = {"t": -1e9, "found": []}


def classify(name: str, uuids) -> tuple:
    """(tipo, servicios) de un anuncio: "trainer", "hr" o None si no es ninguno.

    Rodillo si el nombre lo sugiere o anuncia Cycling Power (1818) o FTMS (1826); banda de FC si
    el nombre lo sugiere o anuncia 180D. El rodillo gana: algunos (p. ej. Wahoo) anuncian ambos.
    """
    low = (name or "").lower()
    us = [str(u).lower() for u in (uuids or [])]
    caps = tuple(label for short, label in _SERVICE_CAPS if any(short in u for u in us))
    is_trainer = any(h in low for h in TRAINER_NAME_HINTS) or "POT" in caps or "FTMS" in caps
    is_hr = any(h in low for h in HR_NAME_HINTS) or "FC" in caps
    if is_trainer:
        return "trainer", caps
    if is_hr:
        return "hr", caps
    return None, caps


async def scan_devices(timeout: float = 6.0, fresh: bool = False) -> list:
    """Escaneo BLE clasificado: lista de `Found`, de mas cerca a mas lejos dentro de cada tipo.

    Los escaneos se SERIALIZAN con un candado por bucle de eventos: BlueZ no admite dos a la
    vez ("operation already in progress") y aqui compiten las dos fuentes, que resuelven su
    dispositivo, y el menu. Un escaneo de hace menos de `SCAN_TTL` s se reutiliza, salvo con
    `fresh=True` (boton Buscar). Usa `return_adv=True` porque bleak >= 1 ya no expone
    `BLEDevice.metadata`: sin el anuncio no hay servicios ni RSSI.
    """
    loop = asyncio.get_running_loop()
    lock = _SCAN_LOCKS.get(loop)
    if lock is None:
        lock = _SCAN_LOCKS[loop] = asyncio.Lock()
    async with lock:
        if not fresh and time.monotonic() - _SCAN_CACHE["t"] < SCAN_TTL:
            return list(_SCAN_CACHE["found"])
        raw = await BleakScanner.discover(timeout=timeout, return_adv=True)
        found = []
        for _addr, (dev, adv) in raw.items():
            name = (dev.name or getattr(adv, "local_name", None) or "").strip()
            kind, caps = classify(name, getattr(adv, "service_uuids", None))
            if kind:
                found.append(Found(name, dev.address, int(getattr(adv, "rssi", None) or -100),
                                   kind, caps))
        found.sort(key=lambda f: (f.kind != "hr", -f.rssi))
        _SCAN_CACHE["t"], _SCAN_CACHE["found"] = time.monotonic(), found
        return list(found)


async def scan_all(timeout: float = 8.0) -> tuple[list, list]:
    """Una sola pasada de escaneo clasificada en (bandas FC, rodillos), de `Found`."""
    found = await scan_devices(timeout)
    return ([f for f in found if f.kind == "hr"], [f for f in found if f.kind == "trainer"])


def friendly_error(text: str) -> str | None:
    """Motivo corto, en mayusculas y ASCII (cabe en la fuente del menu), de un error de Bluetooth
    conocido a partir de su texto; None si no se reconoce."""
    low = (text or "").lower()
    if "dbus" in low or "org.bluez" in low or "no such file" in low or "connection refused" in low:
        return "BLUETOOTH NO DISPONIBLE"
    if "turned off" in low or "poweredoff" in low or "not powered" in low:
        return "BLUETOOTH APAGADO"
    if "permission" in low or "authoriz" in low or "denied" in low:
        return "SIN PERMISO DE BLUETOOTH"
    if "adapter" in low:
        return "SIN ADAPTADOR BLUETOOTH"
    return None


def describe_scan_error(exc: BaseException) -> str:
    """Motivo corto de un escaneo fallido (ver `friendly_error`)."""
    if isinstance(exc, (FileNotFoundError, ConnectionRefusedError)):
        return "BLUETOOTH NO DISPONIBLE"
    return friendly_error(f"{type(exc).__name__} {exc}") or ("ERROR: " + type(exc).__name__.upper())[:30]


def is_address(target: str) -> bool:
    """MAC (BlueZ) o UUID de CoreBluetooth, no un nombre: se conecta directo, sin escanear."""
    return bool(target) and (":" in target or len(target) == 36 or len(target) > 20)


async def scan_heart_rate_sensors(timeout: float = 8.0) -> list:
    hrs, _ = await scan_all(timeout)
    return hrs


async def scan_trainers(timeout: float = 8.0) -> list:
    _, trs = await scan_all(timeout)
    return trs


def _find_char(client, short_uuid: str) -> str | None:
    """Busca una caracteristica en toda la tabla GATT (CoreBluetooth a veces
    entrega variantes del UUID o la resolucion llega parcial)."""
    try:
        for s in client.services:
            for ch in s.characteristics:
                if short_uuid in ch.uuid.lower():
                    return ch.uuid
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------- fuentes --
class _BLESource:
    """Bucle comun: resolver -> conectar -> _setup (suscripciones) -> mantener/reconectar."""

    def __init__(self, target: str | None = None):
        self.target = target
        self.name: str = target or "escaneando..."
        self.address: str | None = None      # direccion real una vez resuelta (para el menu)
        self.connected: bool = False
        self.error: str = ""
        self.last_update: float = 0.0
        self._stop: asyncio.Event | None = None
        self._client: BleakClient | None = None

    async def _scan(self) -> list:
        return await scan_heart_rate_sensors(timeout=6.0)

    async def _setup(self, client: BleakClient) -> None:
        raise NotImplementedError

    async def _resolve(self) -> str | None:
        t = self.target
        if t and is_address(t):
            self.address = t
            return t                                   # MAC o UUID de CoreBluetooth
        candidates = await self._scan()
        dev = None
        if t:
            for c in candidates:
                if t.lower() in (c.name or "").lower():
                    dev = c
                    break
        if dev is None and candidates:
            dev = candidates[0]
        if dev is not None:
            self.name = dev.name or dev.address
            self.address = dev.address
            return dev.address
        return None

    async def _connect_once(self) -> None:
        address = await self._resolve()
        if address is None:
            self.error = "dispositivo no encontrado (¿encendido y en modo TX?)"
            await asyncio.sleep(3.0)
            return
        self.error = ""
        client = BleakClient(address)
        try:
            await client.connect(timeout=15.0)
        except asyncio.CancelledError:
            # Cancelada a medio conectar (el menu cambia de sensor mientras la del arranque aun
            # conecta): sin cerrar el intento, CoreBluetooth deja el dispositivo "conectando" y la
            # fuente nueva no recibe notificaciones.
            try:
                await asyncio.wait_for(asyncio.shield(client.disconnect()), 3.0)
            except BaseException:  # noqa: BLE001 - limpieza; se re-lanza la cancelacion
                pass
            raise
        except Exception as e:  # noqa: BLE001
            self.error = f"no se pudo conectar: {e}"
            await asyncio.sleep(3.0)
            return
        self._client = client
        self.connected = True
        try:
            await self._setup(client)
            while self._stop is not None and not self._stop.is_set() \
                    and self.connected and client.is_connected:
                await asyncio.sleep(1.0)
        finally:
            self.connected = False
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def run(self, stop: asyncio.Event) -> None:
        self._stop = stop
        while not stop.is_set():
            try:
                await self._connect_once()
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                self.connected = False
                await asyncio.sleep(2.0)
        self.connected = False

    def request_reconnect(self) -> None:
        self.error = ""
        if self._client is not None:
            asyncio.ensure_future(self._client.disconnect())


class SensorSource(_BLESource):
    """Banda/pulsera de FC (servicio 180D, caracteristica 2A37)."""

    def __init__(self, target: str | None = None):
        super().__init__(target)
        self.latest_hr: int | None = None
        self.contact: bool | None = None
        self.battery: int | None = None

    def _on_hr(self, _handle, data: bytearray) -> None:
        try:
            hr, contact = parse_hr_measurement(bytearray(data))
        except IndexError:
            return
        if 20 <= hr <= 300:
            self.latest_hr = hr
            self.contact = contact
            self.last_update = time.time()

    def _on_battery(self, _handle, data: bytearray) -> None:
        try:
            self.battery = int(data[0])
        except (IndexError, ValueError):
            pass

    async def _setup(self, client: BleakClient) -> None:
        hr = _find_char(client, HR_CHAR_S)
        if hr is None:
            self.error = "GATT sin 2A37, reintentando..."
            raise ConnectionError("hr characteristic not found")
        await client.start_notify(hr, self._on_hr)
        try:
            bat = _find_char(client, BAT_CHAR_S)
            if bat:
                await asyncio.wait_for(client.read_gatt_char(bat), timeout=3.0)
                await client.start_notify(bat, self._on_battery)
        except Exception:  # noqa: BLE001 - muchas bandas no tienen bateria
            pass


class TrainerSource(_BLESource):
    """Rodillo Tacx u otro: Cycling Power (2A63), CSC (2A5B) y/o Indoor Bike (2ACF)."""

    def __init__(self, target: str | None = None, wheel_mm: int = 2135):
        super().__init__(target)
        self.watts: int | None = None
        self.cadence: float | None = None
        self.speed_kmh: float | None = None
        self.dist_m: int | None = None
        self.energy_kcal: int | None = None
        self.wheel_mm = wheel_mm
        self.source = ""                      # que caracteristicas respondieron
        self.raw_log: list[str] = []          # ultimos payloads (para --probe)
        self._prev_crank = None
        self._prev_wheel = None
        self._prev_csc_crank = None
        self._dist_base = None

    async def _scan(self) -> list:
        return await scan_trainers(timeout=6.0)

    def _on_cp(self, _handle, data: bytearray) -> None:
        d = bytearray(data)
        watts, crank, flags = parse_cycling_power(d)
        # formato Satori (crank no declarado, u16): modulo 16 bits
        rev_mod = 65536 if (crank and not flags & 0x0002) else 1 << 32
        cad = cadence_from_crank(self._prev_crank, crank, rev_mod)
        if crank:
            self._prev_crank = crank
        if watts is not None and 0 <= watts <= 3000:
            self.watts = watts
        if cad is not None and 0 < cad < 300:
            self.cadence = round(cad, 1)
            if "cp" not in self.source:
                self.source = (self.source + " cp").strip()
        if watts is not None or crank is not None:
            self.last_update = time.time()
            self._note_flags("cp", flags)

    def _on_csc(self, _handle, data: bytearray) -> None:
        vals = parse_csc(bytearray(data))
        wr, wt = vals.get("wheel_revs"), vals.get("wheel_time")
        if wr is None or wr > (1 << 28):
            return
        wm = self.wheel_mm / 1000.0
        if self._dist_base is None:
            self._dist_base = wr * wm
        prev = self._prev_wheel
        self._prev_wheel = (wr, wt)
        self.dist_m = max(0, int(wr * wm - self._dist_base))
        if prev:
            d_rev = wr - prev[0]
            d_t = (((wt - prev[1]) % 65536) / 1024.0) if (wt is not None
                                                           and prev[1] is not None) else 1.0
            if 0 <= d_rev <= 20 and 0.05 < d_t <= 10.0:
                kmh = d_rev * wm / d_t * 3.6
                self.speed_kmh = round(kmh, 1)
        if self.cadence is None and vals.get("crank_revs") is not None \
                and vals["crank_revs"] < (1 << 24):
            cc = vals.get("crank_time")
            cad = None
            if self._prev_csc_crank and cc is not None:
                pv, pt = self._prev_csc_crank
                d_r = (vals["crank_revs"] - pv) % 65536
                d_t = ((cc - pt) % 65536) / 1024.0
                if 0 < d_r <= 20 and 0.05 < d_t < 10:
                    cad = d_r / d_t * 60.0
            if cc is not None:
                self._prev_csc_crank = (vals["crank_revs"], cc)
            if cad:
                self.cadence = round(cad, 1)
        self.last_update = time.time()
        if "csc" not in self.source:
            self.source = (self.source + " csc").strip()

    def _on_ftms(self, _handle, data: bytearray) -> None:
        d = bytearray(data)
        out = parse_indoor_bike(d)
        if "speed_kmh" in out:
            self.speed_kmh = round(out["speed_kmh"], 1)
        if "cadence" in out:
            self.cadence = out["cadence"]
            if "bike" not in self.source:
                self.source = (self.source + " bike").strip()
        if "dist_m" in out:
            self.dist_m = out["dist_m"]
        if "energy_kcal" in out:
            self.energy_kcal = out["energy_kcal"]
        self.last_update = time.time()
        self._note_flags("ftms", out.get("_flags", 0))

    def _note_flags(self, tag: str, flags: int) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp} {tag} flags/data=0x{flags & 0xFFFFFFFF:08x}"
        self.raw_log.append(line)
        del self.raw_log[:-20]

    async def _setup(self, client: BleakClient) -> None:
        cp = _find_char(client, CP_CHAR_S)
        ft = _find_char(client, FTMS_BIKE_CHAR_S)
        csc = _find_char(client, CSC_CHAR_S)
        if not cp and not ft:
            self.error = "rodillo sin 2A63/2ACF, reintentando..."
            raise ConnectionError("trainer characteristics not found")
        if cp:
            await client.start_notify(cp, self._on_cp)
        if ft:
            await client.start_notify(ft, self._on_ftms)
        if csc and not ft:                     # con FTMS el CSC es redundante
            await client.start_notify(csc, self._on_csc)
        self.source = (" ".join(x for x in
                                 ((("cp") if cp else ""), (("ftms") if ft else ""),
                                  (("csc") if (csc and not ft) else "")) if x)).strip()


# ---------------------------------------------------------------- simulador -
class SimSensor:
    """Sensor + rodillo simulados: curva tipica de entrenamiento con ruido."""

    name = "SIMULADOR"
    is_sim = True
    address = None
    connected = True
    contact = True
    battery = 85
    error = ""
    source = "sim cp+ftms"

    def __init__(self):
        self.latest_hr: int | None = None
        self.watts: int | None = None
        self.cadence: float | None = None
        self.speed_kmh: float | None = None
        self.dist_m: int = 0
        self.energy_kcal: int = 0
        self.last_update = time.time()
        self._t0 = time.time()
        self._hr = 62.0

    def _hr_at(self, t: float) -> float:
        if t < 20:
            v = 62.0
        elif t < 90:
            v = 62 + (135 - 62) * (t - 20) / 70
        elif t < 330:
            phase = (t - 90) % 120
            v = (148 + 14 * math.sin((phase / 60) * math.pi)
                 if phase < 60 else 96 + 8 * math.sin((phase / 60) * math.pi))
        else:
            v = max(62, 150 - (t - 330) * 0.6)
        return v

    def reset_clock(self) -> None:
        """Reinicia la curva y los acumulados: la simulacion empieza al pulsar Play, no al abrir
        el menu."""
        self._t0 = time.time()
        self._hr = 62.0
        self.dist_m = 0
        self.energy_kcal = 0

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            t = time.time() - self._t0
            self._hr = max(45.0, min(200.0, self._hr_at(t) + random.uniform(-2.5, 2.5)))
            self.latest_hr = int(self._hr)
            self.watts = int(max(0, min(600, 50 + (self._hr - 70) * 3.1
                                        + random.uniform(-12, 12))))
            self.cadence = round(max(0, 84 + 8 * math.sin(t / 23)
                                     + random.uniform(-2, 2)), 1)
            self.speed_kmh = round(max(0, self.watts * 0.125 + random.uniform(-1.2, 1.2)), 1)
            self.dist_m += int(self.speed_kmh / 3.6)          # m por segundo
            self.energy_kcal = int(t * self.watts * 0.00053)
            self.last_update = time.time()
            await asyncio.sleep(0.5)


# -------------------------------------------------------------------- probe -
async def sim_scan(timeout: float = 6.5, fresh: bool = True) -> list:
    """Escaneo de demostracion (`--simulate`): dos dispositivos de mentira tras una pausa corta."""
    await asyncio.sleep(min(1.2, max(0.0, timeout)))
    return [Found("Banda FC (demo)", "demo-hr", -52, "hr", ("FC",)),
            Found("Rodillo (demo)", "demo-trainer", -61, "trainer", ("POT", "FTMS"))]


def sim_factory(slot: str, dev) -> "SimSensor":
    """Fuente para `--simulate`: todo lo que se elija en el menu es un `SimSensor`."""
    src = SimSensor()
    if dev:
        src.name = dev.get("name") or src.name
        src.address = dev.get("address")
    return src


async def probe(target: str | None = None, seconds: float = 12.0,
                trainer: bool = False) -> None:
    """Se suscribe a TODAS las caracteristicas notify y vuelca hex + parseos.

    Uso: hrdash --probe 12 [--trainer|--sensor NOMBRE]
    """
    t0 = time.time()
    hrs, trs = await scan_all(seconds)
    devices = trs if trainer else hrs
    dev_addr = None
    if target:
        for d in devices:
            if target.lower() in (d.name or "").lower() or target == d.address:
                dev_addr = d.address
                break
        if dev_addr is None and (":" in target or len(target) > 20):
            dev_addr = target            # direccion MAC o UUID CoreBluetooth directo
    elif devices:
        dev_addr = devices[0].address
    if dev_addr is None:
        print("Ningun dispositivo candidato en el escaneo "
              "(¿encendido y en modo BLE?).")
        return
    print(f"Probeando {dev_addr} durante {seconds:.0f}s...")
    async with BleakClient(dev_addr, timeout=20) as c:
        print("GATT:")
        for s in c.services:
            print(f"  SERVICIO {s.uuid}")
            for ch in s.characteristics:
                print(f"    CHR {ch.uuid} {list(ch.properties)}")
        subs = []
        for s in c.services:
            for ch in s.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    uuid = ch.uuid
                    short = uuid.split("-")[0][-4:].lower()

                    def make_cb(short_uuid):
                        def cb(_h, data):
                            d = bytearray(data)
                            el = time.time() - t0
                            hexs = bytes(d).hex()
                            extra = ""
                            if short_uuid == HR_CHAR_S and len(d) >= 2:
                                try:
                                    hr, ct = parse_hr_measurement(d)
                                    extra = f" -> FC={hr} contacto={ct}"
                                except Exception:  # noqa: BLE001
                                    pass
                            elif short_uuid == CP_CHAR_S and len(d) >= 4:
                                w, crank, fl = parse_cycling_power(d)
                                extra = f" -> W={w} flags=0x{fl:04x} crank={crank}"
                            elif short_uuid == CSC_CHAR_S and len(d) >= 3:
                                extra = " -> " + str(parse_csc(d))
                            elif short_uuid == FTMS_BIKE_CHAR_S and len(d) >= 2:
                                extra = " -> " + str(parse_indoor_bike(d))
                            print(f"[{el:5.1f}s] {short_uuid}: {hexs}{extra}", flush=True)
                        return cb
                    await c.start_notify(uuid, make_cb(short))
                    subs.append(uuid)
        print(f"suscripto a {len(subs)} caracteristicas, mové el rodillo/pedaleá unos segundos...")
        while time.time() - t0 < seconds + 6:
            await asyncio.sleep(0.5)
        for uuid in subs:
            try:
                await c.stop_notify(uuid)
            except Exception:  # noqa: BLE001
                pass
