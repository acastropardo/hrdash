"""Canal WebSocket para clientes graficos externos (Godot): `hrdash --stream`.

`StreamHost` tiene la misma interfaz que `glview.GLHost` (start / alive / push / drain / stop y
`error`), asi que `cmd_3d` lo usa sin tocar el motor: en vez de lanzar una ventana OpenGL en un
proceso hijo, sirve el mismo snapshot por WebSocket en 127.0.0.1 y recoge los mensajes del cliente.
Los dos clientes (la ventana OpenGL y Godot) hablan el mismo protocolo (`proto.py`); cada arreglo
de BLE, FIT o plan se hace una vez, en el motor.

Contrato (aditivo; un cambio incompatible sube `proto.PROTOCOL`):

  motor -> cliente (un JSON por mensaje)
    {"hello": {"protocol": N, "views": [...], "app": "hrdash"}}   al conectar
    snapshot (el dict de `proto.build_snapshot` mas `version`)    a 1 Hz (2 Hz en el menu)
    {"summary": {...}}                                            al salir, tras exportar

  cliente -> motor
    {"hello": {"client": "godot", "views": [...]}}   el motor cuenta la ventana como abierta
    {"key": "q|x|s|space"}, {"views": [...]}, {"zoom": ...}
    {"ui": {"act": "<id>"}}                          acciones del menu (`Engine.handle_ui`)

El servidor corre en un hilo propio con su bucle de asyncio: el motor (asyncio en el hilo
principal) nunca espera a la red. Aguanta sin cliente (el motor sigue muestreando), con varios
clientes y con clientes que se caen; al conectar, el cliente recibe de inmediato el ultimo
snapshot, para que el menu aparezca sin esperar al siguiente tick.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time

from . import proto

DEFAULT_PORT = 8765
SEND_TIMEOUT = 1.0               # un cliente que no lee no frena al motor ni a los demas


class StreamHost:
    """Servidor WebSocket local con la interfaz de `GLHost` (ver el docstring del modulo)."""

    keeps_open = True            # cmd_3d no lo cierra antes de exportar: el resumen sale por aqui
    label = "stream"

    def __init__(self, port: int = DEFAULT_PORT, views=(), bind: str = "127.0.0.1"):
        self.port = int(port)
        self.bind = bind
        self.views = list(views)
        self.error = ""
        self.dual = False
        self.client_views: list = []         # vistas que declara el ultimo `hello` del cliente
        self.label = f"stream ws://{bind}:{self.port}"
        self._thread = None
        self._loop = None
        self._stop_evt = None
        self._clients: set = set()
        self._inbox: queue.SimpleQueue = queue.SimpleQueue()
        self._ready = threading.Event()
        self._last = None                    # ultimo snapshot serializado
        self._version = 0

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> bool:
        """Levanta el servidor; False si falta `websockets` o el puerto esta ocupado."""
        try:
            import websockets  # noqa: F401
        except Exception:  # noqa: BLE001 - el extra es opcional: avisar como instalarlo
            self.error = '--stream necesita websockets: pip install -e ".[stream]"'
            return False
        self.stop()
        self.error = ""
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="hrdash-stream", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        return self.alive() and not self.error

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        loop, evt, thread = self._loop, self._stop_evt, self._thread
        self._thread = None
        if loop is not None and evt is not None:
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:             # el bucle ya estaba cerrado
                pass
        if thread is not None:
            thread.join(timeout=2.0)
        self._loop = self._stop_evt = None

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001 - el servidor no puede tumbar al motor
            self.error = self.error or f"stream: {type(exc).__name__}: {exc}"[:90]
        finally:
            self._ready.set()

    async def _main(self) -> None:
        import websockets
        self._loop = asyncio.get_running_loop()
        self._stop_evt = asyncio.Event()
        try:
            server = await websockets.serve(self._handler, self.bind, self.port)
        except OSError as exc:
            self.error = f"stream: puerto {self.port}: {exc.strerror or exc}"[:90]
            return
        self._ready.set()
        try:
            await self._stop_evt.wait()
        finally:
            server.close()
            await server.wait_closed()

    # ------------------------------------------------------------ conexiones
    async def _handler(self, ws) -> None:
        self._clients.add(ws)
        try:
            await ws.send(proto.encode({"hello": {"protocol": proto.PROTOCOL,
                                                  "views": self.views, "app": "hrdash"}}))
            if self._last is not None:
                await ws.send(self._last)
            async for raw in ws:
                got = proto.decode(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                if not got:
                    continue
                hello = got.get("hello")
                if isinstance(hello, dict):
                    cv = hello.get("views")
                    self.client_views = [v for v in cv if isinstance(v, str)] if isinstance(cv, list) else []
                    self._inbox.put({"win": True, "client": str(hello.get("client") or "")})
                else:
                    self._inbox.put(got)
        except Exception:  # noqa: BLE001 - una desconexion no puede tumbar el motor
            pass
        finally:
            self._clients.discard(ws)

    async def _broadcast(self, text: str) -> None:
        async def one(ws):
            try:
                await asyncio.wait_for(ws.send(text), SEND_TIMEOUT)
            except Exception:  # noqa: BLE001 - un cliente roto o lento se descarta
                self._clients.discard(ws)
        if self._clients:
            await asyncio.gather(*(one(ws) for ws in list(self._clients)))

    # ------------------------------------------------------------ interfaz de GLHost
    def push(self, snap: dict) -> None:
        """Difunde un snapshot sin bloquear: se queda tambien como el ultimo, para quien conecte."""
        self._version += 1
        snap["version"] = self._version
        text = proto.encode(snap)
        self._last = text
        loop = self._loop
        if loop is None or not self.alive():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(text), loop)
        except Exception as exc:  # noqa: BLE001
            self.error = f"stream push: {type(exc).__name__}"[:90]

    def drain(self) -> list:
        """Mensajes del cliente desde la ultima vez (sin bloquear)."""
        msgs = []
        try:
            while True:
                msgs.append(self._inbox.get_nowait())
        except queue.Empty:
            pass
        return msgs

    def send_summary(self, summary: dict) -> None:
        """Resumen de sesion a los clientes conectados; espera a que salga antes de cerrar."""
        loop = self._loop
        if loop is None or not self.alive():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._broadcast(proto.encode({"summary": summary})), loop)
            fut.result(timeout=SEND_TIMEOUT + 1.0)
            time.sleep(0.3)                  # margen para que el cliente lea el resumen antes del cierre
        except Exception as exc:  # noqa: BLE001 - sin cliente o roto: la sesion ya esta exportada
            self.error = f"stream summary: {type(exc).__name__}"[:90]


def summary_dict(eng, res: dict) -> dict:
    """Resumen de sesion como mensaje (no hay consola que lo imprima en el cliente)."""
    sess = eng.session
    return {
        "n": int(sess.n), "elapsed_s": int(sess.elapsed_s),
        "avg_hr": float(sess.avg_hr), "max_hr": int(sess.max_hr),
        "avg_watts": float(sess.avg_watts), "max_watts": int(sess.max_watts),
        "total_kcal": float(sess.total_kcal), "dist_km": float(sess.dist_km),
        "wkg": float(sess.wkg),
        "fit": str(res.get("fit")) if res.get("fit") else None,
        "csv": str(res.get("csv")) if res.get("csv") else None,
        "fit_error": res.get("fit_error"),
        "validate_ok": bool((res.get("validate") or {}).get("ok")) if res.get("validate") else None,
    }
