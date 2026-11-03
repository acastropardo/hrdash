"""Lectura de sensores de FC por BLE (perfil estandar Heart Rate 0x180D).

Incluye un simulador para probar el dashboard sin hardware.
Compatible macOS (CoreBluetooth) y Linux (BlueZ) via bleak.
"""

from __future__ import annotations

import asyncio
import math
import random
import time

from bleak import BleakClient, BleakScanner

# UUIDs SIG estandar
HR_SERVICE = "0000180d-0000-1100-8000-00805f9b34fb"
HR_CHAR = "00002a37-0000-1100-8000-00805f9b34fb"
BATTERY_SERVICE = "0000180f-0000-1100-8000-00805f9b34fb"
BATTERY_CHAR = "00002a19-0000-1100-8000-00805f9b34fb"

# Nombres habituales de bandas con perfil Heart Rate
HR_NAME_HINTS = ("rhythm", "r+", "scosche", "polar", "hrm", "heart", "h10", "h9",
                 "coospo", "mova", "spovan", "decathlon", "kenafit", "wahoo",
                 "tickr", "orion", "band", "cardio")


def parse_hr_measurement(data: bytearray) -> tuple[int, bool | None]:
    """Devuelve (fc, contacto) desde el payload de la caracteristica 2A37."""
    flags = data[0]
    if flags & 0x01:
        hr = int.from_bytes(data[1:3], "little") & 0x0FFF  # uint16, 12 bits
    else:
        hr = data[1]                                        # uint8
    contact: bool | None = None
    if flags & 0x02:  # sensor contact soportado
        contact = bool(flags & 0x04)
    return hr, contact


async def scan_heart_rate_sensors(timeout: float = 8.0) -> list:
    """Escanea y devuelve devices que parecen sensores de FC (nombre o servicio)."""
    devices = await BleakScanner.discover(timeout=timeout)
    found = []
    for d in devices:
        name = d.name or ""
        md = getattr(d, "metadata", None)
        uuids = [str(u).lower() for u in (md.service_uuids or [])] if md is not None else []
        n = name.lower()
        if HR_SERVICE in uuids or any(h in n for h in HR_NAME_HINTS):
            found.append(d)
    return found


class SensorSource:
    """Conecta a un sensor BLE de FC y mantiene latest_hr actualizado + reconexion."""

    def __init__(self, target: str | None = None):
        self.target = target          # direccion / UUID / fragmento de nombre
        self.name: str = target or "escaneando..."
        self.connected: bool = False
        self.latest_hr: int | None = None
        self.contact: bool | None = None
        self.battery: int | None = None
        self.last_update: float = 0.0
        self.error: str = ""
        self._stop: asyncio.Event | None = None
        self._client: BleakClient | None = None

    # ---- notificaciones ----
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

    @staticmethod
    def _find_char(client, short_uuid: str) -> str | None:
        """Busca una caracteristica en toda la tabla GATT (la resolucion de
        CoreBluetooth a veces llega parcial o con variantes del UUID)."""
        try:
            for s in client.services:
                for ch in s.characteristics:
                    if short_uuid in ch.uuid.lower():
                        return ch.uuid
        except Exception:  # noqa: BLE001
            pass
        return None

    # ---- resolucion de destino ----
    async def _resolve(self) -> str | None:
        if self.target:
            t = self.target
            if ":" in t or (len(t) == 36 and "-" in t) or len(t) > 20:
                return t  # direccion MAC o UUID de CoreBluetooth
        dev = await BleakScanner.find_device_by_name(self.target or "", timeout=6.0) \
            if self.target else None
        if dev is None:
            # escaneo abierto: buscar por pista de nombre o servicio HR
            candidates = await scan_heart_rate_sensors(timeout=6.0)
            if self.target:
                for c in candidates:
                    if self.target.lower() in (c.name or "").lower():
                        dev = c
                        break
            if dev is None and candidates:
                dev = candidates[0]
        if dev is not None:
            self.name = dev.name or dev.address
            return dev.address
        return None

    async def _connect_once(self) -> None:
        address = await self._resolve()
        if address is None:
            self.error = "sensor no encontrado (¿esta puesto y encendido?)"
            await asyncio.sleep(3.0)
            return
        self.error = ""
        client = BleakClient(address)
        try:
            await client.connect(timeout=15.0)
        except Exception as e:  # noqa: BLE001
            self.error = f"no se pudo conectar: {e}"
            await asyncio.sleep(3.0)
            return
        self._client = client
        self.connected = True
        try:
            hr_char = self._find_char(client, "2a37")
            if hr_char is None:
                # tabla GATT incompleta (quirk de CoreBluetooth/Scosche): reconectar
                self.error = "GATT incompleto, reintentando..."
                raise ConnectionError("hr characteristic not found")
            await client.start_notify(hr_char, self._on_hr)
            try:
                bat_char = self._find_char(client, "2a19")
                if bat_char:
                    await asyncio.wait_for(client.read_gatt_char(bat_char), timeout=3.0)
                    await client.start_notify(bat_char, self._on_battery)
            except Exception:  # noqa: BLE001 - la mayoria de bandas no tiene bateria
                pass
            while self.connected and client.is_connected and \
                    self._stop is not None and not self._stop.is_set():
                await asyncio.sleep(1.0)
        finally:
            self.connected = False
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def run(self, stop: asyncio.Event) -> None:
        """Bucle principal: conecta, mantiene, reconecta hasta que `stop`."""
        self._stop = stop
        while not stop.is_set():
            try:
                await self._connect_once()
            except Exception as e:  # noqa: BLE001
                self.error = f"error BLE: {e}"
                self.connected = False
                await asyncio.sleep(3.0)
        self.connected = False

    def request_reconnect(self) -> None:
        self.error = ""
        if self._client is not None:
            asyncio.ensure_future(self._client.disconnect())


class SimSensor:
    """Sensor simulado: curva tipica de entrenamiento con ruido."""

    name = "SIMULADOR"
    connected = True
    contact = True
    battery = 85
    error = ""

    def __init__(self):
        self.latest_hr: int | None = None
        self.last_update = time.time()
        self._t0 = time.time()

    def _hr_at(self, t: float) -> int:
        # reposo -> calentamiento -> intervalos duros con recuperacion -> enfriamiento
        base = 62.0
        if t < 20:
            v = base
        elif t < 90:
            v = base + (135 - base) * (t - 20) / 70
        elif t < 330:
            phase = (t - 90) % 120
            if phase < 60:
                v = 148 + 14 * math.sin((phase / 60) * math.pi)
            else:
                v = 96 + 8 * math.sin((phase / 60) * math.pi)
        else:
            v = max(base, 150 - (t - 330) * 0.6)
        v += random.uniform(-2.5, 2.5)
        return int(max(35, min(210, v)))

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.latest_hr = self._hr_at(time.time() - self._t0)
            self.last_update = time.time()
            await asyncio.sleep(0.5)
