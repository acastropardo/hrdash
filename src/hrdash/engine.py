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

    def __init__(self, hr_source, tr_source, config: Config):
        self.hr = hr_source
        self.tr = tr_source
        self.config = config
        self.session = Session(config)
        self.laps = LapTracker(config.lap_m, config)
        self.views: list[str] = ["gauge"]   # vistas 3D; en standalone la ventana las modifica
        self._stop = asyncio.Event()
        self._tasks: list = []

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
        hr = getattr(self.hr, "latest_hr", None)
        if hr is None:
            return (None, None)
        w = getattr(self.tr, "watts", None) if self.tr else None
        cad = getattr(self.tr, "cadence", None) if self.tr else None
        spd = getattr(self.tr, "speed_kmh", None) if self.tr else None
        dist = getattr(self.tr, "dist_m", None) if self.tr else None
        s = self.session.add(hr, watts=w, cadence=cad, speed_kmh=spd, dist_m=dist)
        return (s, self.laps.add(s))

    def snapshot(self, zoom_cmd=(0, 0)) -> dict:
        """Snapshot para el proceso de la ventana, con el mismo builder que usa la TUI."""
        return proto.build_snapshot(self.session, self.config, self.laps,
                                   self.hr, self.tr,
                                   view=self.views[0] if self.views else "hr",
                                   views=self.views, zoom_cmd=zoom_cmd,
                                   standalone=True)

    def handle_key(self, name: str) -> bool:
        """Tecla procesada por el motor en standalone. True si fue `q` (salir y grabar).

        `q`/`x`/`s` afectan a la sesion y por eso el hijo las reenvia. En standalone el
        motor es la dueña; con TUI la dueña sigue siendo `ui.py` (no se duplica aqui).
        """
        if name == "q":
            return True
        if name == "x":
            self.session.reset()
            self.laps.reset()
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
