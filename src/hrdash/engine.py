"""Motor de la sesion, sin UI: fuentes + Session + LapTracker + export.

Lo manejan `cmd_headless` (`--duration`) y `cmd_3d` (`--3d`). El motor no dibuja ni
renderiza nada: lee los atributos que las fuentes BLE escriben desde sus callbacks
(`latest_hr`, `watts`, `cadence`, `speed_kmh`, `dist_m`), el mismo poll de 1 Hz que hace
la TUI en `tick()`.

Por eso se puede verificar sin display y sin TTY, y por eso el modo `--3d` no colisiona
con la consola: la ventana es un proceso hijo que habla JSON por tuberia (proto.py), y
el teclado de la ventana no se reenvia a un terminal porque en standalone no hay TUI.

El `poll` no puede tumbar el muestreo y el export debe sobrevivir a todo: export se
escribe siempre antes que el FIT (CSV primero dentro de `export_session`).
"""

from __future__ import annotations

import asyncio

from . import proto, workouts
from .config import Config
from .history import Session
from .laps import LapTracker

SCENES_VIEWS = ("hr", "watts", "gauge", "pista")


MAX_DEVICES = 7                     # filas de dispositivos en el menu (mas la fila del simulador)
SCAN_SECS = 6.5


async def _default_scanner(timeout: float, fresh: bool = True) -> list:
    from dataclasses import asdict
    from .ble import scan_devices
    return [asdict(f) for f in await scan_devices(timeout, fresh)]


class Engine:
    """Dueño del estado de la sesion para cualquier frontend (TUI, headless, ventana 3D).

    Cada fuente (banda de FC, rodillo) corre en su propia tarea con su propio evento de parada,
    asi el menu puede cambiar una sin tocar la otra. `scanner(timeout, fresh)` y
    `factory(slot, dev)` son inyectables: el menu se prueba sin Bluetooth.
    """

    def __init__(self, hr_source, tr_source, config: Config, menu: bool = False,
                 scanner=None, factory=None):
        self.hr = hr_source
        self.tr = tr_source
        self.config = config
        self.session = Session(config)
        self.laps = LapTracker(config.lap_m, config)
        self.views: list[str] = ["gauge"]   # vistas 3D; en standalone la ventana las modifica
        # "menu": las fuentes conectan y se ven en vivo, pero no se anota nada hasta `begin()`.
        # Sin menu (`--now`, headless) arranca ya en "running", como siempre.
        self.phase = "menu" if menu else "running"
        self._dist0 = 0                     # distancia del rodillo al empezar la sesion
        self.scanner = scanner or _default_scanner
        self.factory = factory or self._default_factory
        self.devices: list[dict] = []       # ultimo escaneo, ya recortado y ordenado
        self.scan: dict = {"state": "idle", "msg": "", "n": 0}
        self._scan_task = None
        self._runs: dict = {}               # id(fuente) -> (fuente, evento de parada, tarea)
        self.trainer_pref: bool | None = None   # el usuario quito (False) o eligio (True) rodillo
        self.spec = config.session              # sesion elegida en el menu (se guarda al salir)
        self.page = "sensores"                  # pagina visible del menu: "sensores" | "sesion"
        self.workout = None                     # plan de la sesion en curso (None = libre sin limite)

    # ------------------------------------------------------------ fuentes
    def _default_factory(self, slot: str, dev):
        """Fuente nueva para una eleccion del menu (`slot` = "hr" | "trainer" | "both")."""
        from .ble import SensorSource, SimSensor, TrainerSource
        if slot == "both" or not dev or dev.get("sim"):
            return SimSensor()
        if slot == "hr":
            src = SensorSource(dev["address"])
        else:
            src = TrainerSource(dev["address"], wheel_mm=self.config.wheel_mm)
        src.name = dev.get("name") or dev["address"]
        src.address = dev["address"]
        return src

    def _ensure_running(self, src) -> None:
        """Arranca la tarea de una fuente si no corre ya (con el simulador compartido por las dos
        ranuras corre UNA vez: dos tareas sobre el mismo objeto duplicaban su distancia)."""
        if src is None or id(src) in self._runs:
            return
        ev = asyncio.Event()
        self._runs[id(src)] = (src, ev, asyncio.create_task(src.run(ev)))

    async def _release_unused(self, everything: bool = False) -> None:
        """Para las fuentes que ya no estan asignadas a ninguna ranura (todas con `everything`)."""
        for key, (src, ev, task) in list(self._runs.items()):
            if not everything and (src is self.hr or src is self.tr):
                continue
            del self._runs[key]
            ev.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def start(self) -> None:
        """Arranca las fuentes como tareas async (los callbacks escriben atributos planos)."""
        self._ensure_running(self.hr)
        self._ensure_running(self.tr)

    async def stop(self) -> None:
        self._cancel_scan()
        task, self._scan_task = self._scan_task, None
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._release_unused(everything=True)    # las ranuras se conservan: export y persist

    def _trainer_dist(self) -> int:
        return int(getattr(self.tr, "dist_m", None) or 0) if self.tr else 0

    # ------------------------------------------------------------ menu: escaneo
    def request_scan(self, fresh: bool = True) -> bool:
        """Lanza un escaneo en segundo plano; False si ya hay uno en curso (no se encolan)."""
        if self.phase != "menu" or (self._scan_task is not None and not self._scan_task.done()):
            return False
        self.scan = {"state": "scanning", "msg": "", "n": 0}
        self._scan_task = asyncio.create_task(self._scan(fresh))
        return True

    def _cancel_scan(self) -> None:
        if self._scan_task is not None and not self._scan_task.done():
            self._scan_task.cancel()

    async def _scan(self, fresh: bool) -> None:
        try:
            found = await self.scanner(SCAN_SECS, fresh)
            devs = []
            for f in (found or []):
                d = dict(f) if isinstance(f, dict) else dict(vars(f))
                if d.get("address") and d.get("kind") in ("hr", "trainer"):
                    d["rssi"] = int(d.get("rssi", -100))
                    devs.append(d)
            devs.sort(key=lambda d: (d["kind"] != "hr", -d["rssi"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - sin Bluetooth/permiso o respuesta rara: se muestra
            from .ble import describe_scan_error
            self.scan = {"state": "error", "msg": describe_scan_error(exc), "n": 0}
            return
        self.devices = devs[:MAX_DEVICES]
        self.scan = {"state": "done", "msg": "", "n": len(devs)}

    # ------------------------------------------------------------ menu: asignacion
    async def _set_slots(self, hr=None, tr=None, keep_hr=True, keep_tr=True) -> None:
        if not keep_hr:
            self.hr = hr
        if not keep_tr:
            self.tr = tr
        self._ensure_running(self.hr)
        self._ensure_running(self.tr)
        await self._release_unused()

    async def assign_device(self, dev_id: str) -> bool:
        """Asigna un dispositivo de la ultima lista: las bandas a FC, los rodillos al rodillo."""
        dev = next((d for d in self.devices if d["address"] == dev_id), None)
        if dev is None:
            return False
        if dev["kind"] == "hr":
            if getattr(self.hr, "address", None) == dev_id:
                return False
            await self._set_slots(hr=self.factory("hr", dev), keep_hr=False)
        else:
            if getattr(self.tr, "address", None) == dev_id:
                return False
            await self._set_slots(tr=self.factory("trainer", dev), keep_tr=False)
            self.trainer_pref = True
        return True

    async def assign_sim(self) -> bool:
        """El simulador (FC + rodillo) en las dos ranuras, para probar sin hardware."""
        if self.hr is self.tr and getattr(self.hr, "is_sim", False):
            return False
        sim = self.factory("both", None)
        await self._set_slots(hr=sim, tr=sim, keep_hr=False, keep_tr=False)
        return True

    async def remove_trainer(self) -> bool:
        if self.tr is None:
            return False
        await self._set_slots(tr=None, keep_tr=False)
        self.trainer_pref = False
        return True

    def play_state(self) -> tuple:
        """(puede iniciar, motivo): hace falta una FC en vivo, porque sin ella no se anota nada."""
        hr = self.hr
        if hr is None:
            return False, "ELIGE UNA BANDA DE FC"
        if getattr(hr, "latest_hr", None) is not None:
            return True, ""
        if getattr(hr, "connected", False):
            return False, "ESPERANDO FC: PONTE LA BANDA"
        from .ble import friendly_error
        err = str(getattr(hr, "error", "") or "").strip()
        return False, ("FC: " + (friendly_error(err) or err.upper())[:34]) if err else \
            "CONECTANDO CON LA BANDA..."

    # ------------------------------------------------------------ fase
    def begin(self) -> None:
        """Pasa de menu a rodando: sesion y vueltas nuevas, simulador reiniciado y la distancia
        del rodillo (que corre desde que se conecto) vuelve a cero."""
        self._cancel_scan()                 # escanear mientras se rueda estorba a las conexiones
        self.workout = self.spec.build()    # el plan elegido; la fase 4 lo sigue durante la sesion
        self.session.reset()
        self.laps.reset()
        for src in (self.hr, self.tr):
            if src is not None and hasattr(src, "reset_clock"):
                src.reset_clock()
        self._dist0 = self._trainer_dist()
        self.phase = "running"

    async def handle_ui(self, ui) -> bool:
        """Evento de la interfaz de la ventana (`{"act": id}`). True si cambio algo y conviene
        mandar un snapshot ya. Solo actua en fase menu."""
        act = ui.get("act") if isinstance(ui, dict) else None
        if not isinstance(act, str) or self.phase != "menu":
            return False
        if act == "play":
            if not self.play_state()[0]:
                return False                # sin FC no se inicia (el menu lo muestra)
            self.begin()
            return True
        if act.startswith("page:"):
            page = act[5:]
            if page in ("sensores", "sesion") and page != self.page:
                self.page = page
                return True
            return False
        new = workouts.apply_act(self.spec, act)   # kind:, min:, min1:, min5:, cu:
        if new is not None:
            changed = new != self.spec
            self.spec = new
            return changed
        if act == "scan":
            return self.request_scan(fresh=True)
        if act == "sim":
            return await self.assign_sim()
        if act == "notrainer":
            return await self.remove_trainer()
        if act.startswith("dev:"):
            return await self.assign_device(act[4:])
        return False

    def menu_info(self) -> dict:
        """Todo lo que dibuja el menu: las dos ranuras (estado y lectura en vivo), los dispositivos
        del ultimo escaneo con su uso actual, el estado del escaneo y si se puede iniciar."""
        def src(s, fields):
            if s is None:
                return None
            from .ble import friendly_error
            err = str(getattr(s, "error", "") or "")
            d = {"name": str(getattr(s, "name", "") or ""), "connected": bool(getattr(s, "connected", False)),
                 "error": friendly_error(err) or err, "sim": bool(getattr(s, "is_sim", False))}
            for k, attr in fields:
                v = getattr(s, attr, None)
                d[k] = None if v is None else (round(float(v), 1) if isinstance(v, float) else int(v))
            return d

        def use(d):
            for slot, s in (("hr", self.hr), ("trainer", self.tr)):
                if s is not None and getattr(s, "address", None) == d["address"]:
                    return slot
            return None

        ok, why = self.play_state()
        return {"hr": src(self.hr, (("hr", "latest_hr"),)),
                "trainer": src(self.tr, (("watts", "watts"), ("cadence", "cadence"), ("speed", "speed_kmh"))),
                "devices": [{"id": d["address"], "name": str(d.get("name") or ""), "rssi": int(d.get("rssi", -100)),
                             "kind": d["kind"], "caps": list(d.get("caps") or []), "use": use(d)}
                            for d in self.devices],
                "scan": dict(self.scan),
                "sim": self.hr is self.tr and bool(getattr(self.hr, "is_sim", False)),
                "can_play": ok, "hint": why, "page": self.page, "session": self.session_info()}

    def session_info(self) -> dict:
        """La sesion elegida tal como la dibuja el menu: la especificacion, el plan que resulta
        (nombre, duracion, lineas de descripcion) y su perfil de bloques para el grafico."""
        s = self.spec
        w = s.build()
        return {**workouts.spec_to_dict(s), "summary": s.summary(),
                "name": w.name if w else "Libre sin limite", "total_s": w.total_s if w else 0,
                "lines": workouts.describe(w), "profile": workouts.profile(w)}

    def poll(self) -> tuple:
        """Un ciclo de 1 Hz: lee atributos, añade muestra y vuelta. (sample, closed).

        (None, None) si la fuente de FC aun no dio datos (esperando FC).
        """
        if self.phase == "menu":
            return (None, None)             # antes de Play se lee en vivo pero no se graba
        hr = getattr(self.hr, "latest_hr", None)
        if hr is None:
            return (None, None)
        w = getattr(self.tr, "watts", None) if self.tr else None
        cad = getattr(self.tr, "cadence", None) if self.tr else None
        spd = getattr(self.tr, "speed_kmh", None) if self.tr else None
        dist = getattr(self.tr, "dist_m", None) if self.tr else None
        if dist is not None:
            dist = max(0, int(dist) - self._dist0)
        s = self.session.add(hr, watts=w, cadence=cad, speed_kmh=spd, dist_m=dist)
        return (s, self.laps.add(s))

    def snapshot(self, zoom_cmd=(0, 0)) -> dict:
        """Snapshot para el proceso de la ventana, con el mismo builder que usa la TUI."""
        menu = self.phase == "menu"
        views = ["menu"] if menu else self.views
        return proto.build_snapshot(self.session, self.config, self.laps,
                                   self.hr, self.tr,
                                   view=views[0] if views else "hr",
                                   views=views, zoom_cmd=zoom_cmd,
                                   standalone=True, phase=self.phase,
                                   menu=self.menu_info() if menu else None)

    def handle_key(self, name: str) -> bool:
        """Tecla procesada por el motor en standalone. True si fue `q` (salir y grabar).

        `q`/`x`/`s` afectan a la sesion y por eso el hijo las reenvia. En standalone el
        motor es la dueña; con TUI la dueña sigue siendo `ui.py` (no se duplica aqui).
        """
        if name == "q":
            return True
        if name == "x":
            if self.phase == "menu":
                return False                # no hay sesion que reiniciar antes de Play
            self.session.reset()
            self.laps.reset()
            self._dist0 = self._trainer_dist()      # reiniciar tambien pone la distancia a 0
        elif name == "s":
            for src in (self.hr, self.tr):
                if src is not None and hasattr(src, "request_reconnect"):
                    src.request_reconnect()
        return False

    def name(self) -> str:
        name = getattr(self.hr, "name", "HRDash")
        if getattr(self.tr, "name", None):
            name += f" + {self.tr.name}"
        return name

    def export(self) -> dict:
        from .export import export_session
        try:
            return export_session(self.session, self.config, self.name())
        except Exception as exc:  # noqa: BLE001 - el export no puede tumbar la salida
            return {"fit_error": str(exc)}
