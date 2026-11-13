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

from . import peloton, proto, race, racelog, workouts
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
                 scanner=None, factory=None, demo: bool = False):
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
        self._pause_dist = 0                # distancia del rodillo al pausar (para no contar la pausa)
        # Dos modos de fuentes: sensores reales (BLE) y demo (simulador, `--simulate`). El menu puede pasar de uno
        # a otro (acciones `sim` y `real`), asi que cada modo guarda su par (escaneo, fabrica). `scanner` y
        # `factory` inyectables (pruebas) sustituyen al par REAL.
        from .ble import sim_factory, sim_scan
        self._real = [scanner or _default_scanner, factory or self._default_factory]
        self._demo = [sim_scan, sim_factory]
        self.demo = bool(demo)
        self.devices: list[dict] = []       # ultimo escaneo, ya recortado y ordenado
        self.scan: dict = {"state": "idle", "msg": "", "n": 0}
        self._scan_task = None
        self._runs: dict = {}               # id(fuente) -> (fuente, evento de parada, tarea)
        self.trainer_pref: bool | None = None   # el usuario quito (False) o eligio (True) rodillo
        self.spec = config.session              # sesion elegida en el menu (se guarda al salir)
        self.page = "sensores"                  # pagina visible del menu: "sensores" | "sesion"
        self.workout = None                     # plan de la sesion en curso (None = libre sin limite)
        self.race = None                        # persecucion en curso (race.PursuitRace) o None
        self.race_note = ""                     # aviso al jugador (p. ej. no hay marca previa para el fantasma)
        self.race_pb: bool | None = None        # al terminar: si fue record personal

    # ------------------------------------------------------------ fuentes
    @property
    def scanner(self):
        return (self._demo if self.demo else self._real)[0]

    @property
    def factory(self):
        return (self._demo if self.demo else self._real)[1]

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
        # Primero se suelta la fuente que sale y despues arranca la nueva: dos conexiones a la
        # vez al mismo dispositivo (la que el arranque resolvio solo y la que se elige en el
        # menu) se pisan en CoreBluetooth y la nueva se queda sin notificaciones.
        await self._release_unused()
        self._ensure_running(self.hr)
        self._ensure_running(self.tr)

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
            self.demo = True
            return False
        self.demo = True          # a partir de aqui el escaneo y las elecciones del menu son de demostracion
        sim = self.factory("both", None)
        await self._set_slots(hr=sim, tr=sim, keep_hr=False, keep_tr=False)
        return True

    async def assign_real(self) -> bool:
        """Sale del simulador y vuelve a los sensores reales: suelta el simulador, recupera la ultima banda
        (y rodillo) guardados en la configuracion y lanza un escaneo para refrescar la lista. Si ya no hay
        simulador solo refresca el escaneo."""
        was_sim = self.demo or getattr(self.hr, "is_sim", False) or getattr(self.tr, "is_sim", False)
        self.demo = False
        if not was_sim:
            return self.request_scan(fresh=True)
        hr = tr = None
        cfg = self.config
        if cfg.last_device:
            hr = self.factory("hr", {"address": cfg.last_device, "name": cfg.last_device})
        if cfg.last_trainer and cfg.use_trainer is not False:
            tr = self.factory("trainer", {"address": cfg.last_trainer, "name": cfg.last_trainer})
            self.trainer_pref = True
        await self._set_slots(hr=hr, tr=tr, keep_hr=False, keep_tr=False)
        self.request_scan(fresh=True)
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
        self.race = None
        self.session.reset()
        self.laps.reset()
        for src in (self.hr, self.tr):
            if src is not None and hasattr(src, "reset_clock"):
                src.reset_clock()
        self._dist0 = self._trainer_dist()
        self.phase = "running"
        if self.spec.kind in ("race", "scratch"):
            self.start_race()

    # ------------------------------------------------------------ carrera (persecucion contra fantasma)
    def start_race(self) -> None:
        """Crea la persecucion elegida y arranca su cuenta atras. El fantasma "mi mejor marca" sale del historial
        (`racelog`); sin marca previa en esa distancia corre el de ritmo y se avisa."""
        if self.spec.kind == "scratch":
            # scratch: el jugador contra un pelotón de IA cuyo FTP sale del FTP de la configuracion
            self.race_note, self.race_pb = "", None
            self.race = peloton.ScratchRace(self.spec.scratch, float(self.config.ftp), float(self.config.weight) + 8.0)
            self.race.start()
            return
        spec = self.spec.race
        phys = race.Physics(mass=float(self.config.weight) + 8.0)     # ciclista + bici
        ghost_power, self.race_note = None, ""
        if spec.ghost == "best":
            prev = racelog.best(spec.distance_m)
            if prev is not None:
                ghost_power = race.series_power(prev["power"])
            else:
                self.race_note = "SIN MARCA PREVIA: FANTASMA DE RITMO"
        self.race = race.PursuitRace(spec, phys, ghost_power)
        self.race_pb = None
        self.race.start()

    def race_active(self) -> bool:
        """Hay una carrera que pide 10 Hz (cuenta atras o en marcha)."""
        return self.race is not None and self.race.state in ("countdown", "running")

    def step_race(self, dt: float) -> None:
        """Avanza la carrera `dt` s con la ultima potencia del rodillo (0 sin lectura). Al terminar guarda la marca."""
        r = self.race
        if r is None or self.phase != "running":
            return
        was = r.state
        w = getattr(self.tr, "watts", None) if self.tr else None
        r.step(dt, float(w) if w is not None else 0.0)
        if was != "finished" and r.state == "finished" and getattr(r, "final_s", None) is not None:
            self.race_pb = racelog.add(racelog.record_from_race(r))

    def race_info(self) -> dict | None:
        """Clave `race` del snapshot: el estado de la carrera mas lo que el cliente necesita aparte."""
        r = self.race
        if r is None or self.phase != "running":
            return None
        d = r.to_dict()
        wheel = getattr(self.tr, "speed_kmh", None) if self.tr else None
        d["wheel_kmh"] = None if wheel is None else round(float(wheel), 1)     # la velocidad real de la rueda, como dato
        if self.race_note:
            d["note"] = self.race_note
        if r.state == "finished" and d.get("event") == "persecucion":
            d["pb"] = bool(self.race_pb)
        return d

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
        if act == "real":
            return await self.assign_real()
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
                "can_real": True,       # este motor sabe volver a los sensores reales (accion `real`)
                "can_play": ok, "hint": why, "page": self.page, "session": self.session_info()}

    def session_info(self) -> dict:
        """La sesion elegida tal como la dibuja el menu: la especificacion, el plan que resulta
        (nombre, duracion, lineas de descripcion) y su perfil de bloques para el grafico."""
        s = self.spec
        w = s.build()
        if s.kind == "race":
            return {**workouts.spec_to_dict(s), "summary": s.summary(), "name": "Persecucion individual",
                    "total_s": 0, "lines": self.race_hint(), "profile": []}
        if s.kind == "scratch":
            sc = s.scratch
            lo, hi = peloton.LEVEL_RANGE[sc.level]
            ftp = int(self.config.ftp)
            return {**workouts.spec_to_dict(s), "summary": s.summary(), "name": "Scratch contra el peloton",
                    "total_s": 0, "profile": [],
                    "lines": [f"{peloton.N_RIVALS} RIVALES DE {int(ftp * lo)} A {int(ftp * hi)} W DE FTP",
                              f"{sc.distance_m() // 1000} KM EN {sc.laps} VUELTAS DE 250 M",
                              "GANA EL PRIMERO EN CRUZAR LA META",
                              "SALIDA PARADA, CUENTA ATRAS DE 3 S"]}
        return {**workouts.spec_to_dict(s), "summary": s.summary(),
                "name": w.name if w else "Libre sin limite", "total_s": w.total_s if w else 0,
                "lines": workouts.describe(w), "profile": workouts.profile(w)}

    def race_hint(self) -> list:
        """Lineas del menu para la persecucion elegida: a que ritmo corre el fantasma o la marca que se bate."""
        sp = self.spec.race
        key = (sp.distance_m, sp.ghost, sp.target_s)
        if getattr(self, "_hint_key", None) != key:
            phys = race.Physics(mass=float(self.config.weight) + 8.0)
            if sp.ghost == "pace":
                p = race.pace_power(phys, sp.distance_m, sp.target_s)
                lines = [f"FANTASMA: {int(round(p))} W DE MEDIA",
                         f"{sp.distance_m / sp.target_s * 3.6:.1f} KM/H DE MEDIA",
                         "SALIDA PARADA, CUENTA ATRAS DE 3 S"]
            else:
                prev = racelog.best(sp.distance_m)
                lines = (["TU MEJOR MARCA: " + race.fmt_clock(prev["time_s"]), prev["date"]] if prev else
                         ["SIN MARCA PREVIA EN ESTA DISTANCIA", "CORRERA EL FANTASMA DE RITMO"])
                lines.append("SALIDA PARADA, CUENTA ATRAS DE 3 S")
            self._hint_key, self._hint = key, lines
        return list(self._hint)

    # ------------------------------------------------------------ plan en curso (fase 4)
    def _plan_pos(self):
        """Posicion del plan a los segundos de sesion que lleva (el mismo reloj que `Session`)."""
        return self.workout.at(self.session.elapsed_s)

    def block_label(self) -> str | None:
        """"Esfuerzo 2/4" del bloque actual; "libre" si el plan ya termino; None sin plan."""
        if self.race is not None and self.phase == "running":
            st = self.race.state
            return "Salida" if st in ("ready", "countdown") else "libre" if st == "finished" \
                else f"Vuelta {self.race.lap()}/{self.race.laps}"
        if self.workout is None or self.phase != "running":
            return None
        pos = self._plan_pos()
        if pos.done:
            return "libre"
        b = pos.slot.block
        rep = pos.slot.rep_label
        return f"{b.name} {rep}".strip() if rep else b.name

    def plan_info(self) -> dict | None:
        """Lo que la ventana dibuja del plan (tira de bloque y arco objetivo); None sin plan.

        Objetivo por potencia si hay lectura de rodillo y el bloque la define; si no, por FC
        (`workouts.resolve_target`). `status` compara la lectura actual: low / ok / high.
        Pasado el final, `done` (el plan avisa y la sesion sigue libre, sin tope)."""
        w = self.workout
        if w is None or self.phase != "running":
            return None
        pos = self._plan_pos()
        info = {"name": w.name, "total": w.total_s, "left_total": pos.left_total,
                "progress": round(pos.progress, 4), "done": pos.done,
                "profile": workouts.profile(w), "n": len(w.slots()),
                "t": int(self.session.elapsed_s)}
        if pos.done:
            info["over"] = max(0, int(self.session.elapsed_s) - w.total_s)
            return info
        slot, b = pos.slot, pos.slot.block
        cfg = self.config
        watts = getattr(self.tr, "watts", None) if self.tr else None
        tgt = workouts.resolve_target(b, int(cfg.ftp), int(cfg.computed_hrmax()), watts is not None)
        target = None
        if tgt is not None:
            val = watts if tgt.unit == "w" else getattr(self.hr, "latest_hr", None)
            target = {"unit": tgt.unit, "lo": tgt.lo, "hi": tgt.hi, "status": tgt.status(val)}
        nxt = pos.next
        info.update({"i": slot.index, "bname": b.name, "kind": b.kind, "rep": slot.rep_label,
                     "elapsed": pos.elapsed, "left": pos.left, "blen": b.secs, "target": target,
                     "pct": workouts.intensity(b),
                     "next": None if nxt is None else {"name": nxt.block.name, "secs": nxt.block.secs}})
        return info

    def toggle_pause(self) -> bool:
        """Pausa o reanuda la sesion en curso (solo rodando). En pausa no se anota nada y el reloj de
        la sesion y del plan se congela; la distancia que el rodillo siga contando se descarta."""
        if self.phase != "running":
            return False
        if self.session.paused:
            self.session.resume()
            self._dist0 += self._trainer_dist() - self._pause_dist
            if self.race is not None:
                self.race.resume()
        else:
            self.session.pause()
            self._pause_dist = self._trainer_dist()
            if self.race is not None:
                self.race.pause()
        return True

    def poll(self) -> tuple:
        """Un ciclo de 1 Hz: lee atributos, añade muestra y vuelta. (sample, closed).

        (None, None) si la fuente de FC aun no dio datos (esperando FC).
        """
        if self.phase == "menu":
            return (None, None)             # antes de Play se lee en vivo pero no se graba
        if self.session.paused:
            return (None, None)             # en pausa tampoco
        hr = getattr(self.hr, "latest_hr", None)
        if hr is None:
            return (None, None)
        w = getattr(self.tr, "watts", None) if self.tr else None
        cad = getattr(self.tr, "cadence", None) if self.tr else None
        spd = getattr(self.tr, "speed_kmh", None) if self.tr else None
        dist = getattr(self.tr, "dist_m", None) if self.tr else None
        if dist is not None:
            dist = max(0, int(dist) - self._dist0)
        if self.race is not None and self.race.state != "ready":
            # en carrera la velocidad y la distancia son las virtuales (de la potencia); la rueda va en `race.wheel_kmh`
            spd = self.race.v * 3.6
            dist = int(self.race.s)
        s = self.session.add(hr, watts=w, cadence=cad, speed_kmh=spd, dist_m=dist,
                             bloque=self.block_label())
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
                                   menu=self.menu_info() if menu else None,
                                   workout=self.plan_info(), paused=self.session.paused,
                                   race=self.race_info())

    def handle_key(self, name: str) -> bool:
        """Tecla procesada por el motor en standalone. True si fue `q` (salir y grabar).

        `q`/`x`/`s` afectan a la sesion y por eso el hijo las reenvia. En standalone el
        motor es la dueña; con TUI la dueña sigue siendo `ui.py` (no se duplica aqui).
        """
        if name == "q":
            return True
        if name == "space":
            self.toggle_pause()
        elif name == "x":
            if self.phase == "menu":
                return False                # no hay sesion que reiniciar antes de Play
            self.session.reset()
            self.laps.reset()
            self._dist0 = self._trainer_dist()      # reiniciar tambien pone la distancia a 0
            if self.race is not None:
                self.start_race()                   # y la carrera vuelve a la cuenta atras
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
        self.session.resume()               # una pausa abierta al salir se cierra antes de grabar
        try:
            return export_session(self.session, self.config, self.name())
        except Exception as exc:  # noqa: BLE001 - el export no puede tumbar la salida
            return {"fit_error": str(exc)}
