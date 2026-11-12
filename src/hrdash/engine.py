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

from . import proto
from .config import Config
from .history import Session
from .laps import LapTracker

SCENES_VIEWS = ("hr", "watts", "gauge", "pista")


class Engine:
    """Dueño del estado de la sesion para cualquier frontend (TUI, headless, ventana 3D)."""

    def __init__(self, hr_source, tr_source, config: Config, menu: bool = False):
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
        self._stop = asyncio.Event()
        self._tasks: list = []

    def _trainer_dist(self) -> int:
        return int(getattr(self.tr, "dist_m", None) or 0) if self.tr else 0

    def begin(self) -> None:
        """Pasa de menu a rodando: sesion y vueltas nuevas, simulador reiniciado y la distancia
        del rodillo (que corre desde que se conecto) vuelve a cero."""
        self.session.reset()
        self.laps.reset()
        for src in (self.hr, self.tr):
            if src is not None and hasattr(src, "reset_clock"):
                src.reset_clock()
        self._dist0 = self._trainer_dist()
        self.phase = "running"

    def handle_ui(self, ui) -> bool:
        """Evento de la interfaz de la ventana (`{"act": ...}`). True si cambio el estado y
        conviene mandar un snapshot ya."""
        act = ui.get("act") if isinstance(ui, dict) else None
        if act == "play" and self.phase == "menu":
            self.begin()
            return True
        return False

    def menu_info(self) -> dict:
        """Lo que muestra el menu de cada fuente (nombre, estado y lectura en vivo)."""
        def src(s, fields):
            if s is None:
                return None
            d = {"name": str(getattr(s, "name", "") or ""), "connected": bool(getattr(s, "connected", False)),
                 "error": str(getattr(s, "error", "") or "")}
            for k, attr in fields:
                v = getattr(s, attr, None)
                d[k] = None if v is None else (round(float(v), 1) if isinstance(v, float) else int(v))
            return d
        return {"hr": src(self.hr, (("hr", "latest_hr"),)),
                "trainer": src(self.tr, (("watts", "watts"), ("cadence", "cadence"), ("speed", "speed_kmh")))}

    def start(self) -> None:
        """Arranca las fuentes como tareas async (los callbacks escriben atributos planos)."""
        self._tasks = [asyncio.create_task(self.hr.run(self._stop))]
        if self.tr is not None:
            self._tasks.append(asyncio.create_task(self.tr.run(self._stop)))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

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
