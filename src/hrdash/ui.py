"""Dashboard a pantalla completa (Textual).

Layout:
  estado de sensores | 4 tiles gigantes (FC / WATTS / km-h / rpm)
  | barras HR 1Hz (1fr) | barras WATTS (4) | lineas VEL/CAD (2x8 niveles)
  | estadisticas (4) | tiempo en zona (6) | ayuda teclas (1)

Vistas: h = barras de FC arriba, w = barras de watts, g = tablero de gauges
(diales braille con agujas animadas que reemplaza tiles/graficos/zonas),
p = pista de carreras: ovalo en braille con la bici en la vuelta de `lap_m` y
tablero de vueltas. El marcador se mueve a 10 fps con su propio timer.
Se navega entre vistas con ← → (en la pista, [ ] cambia el largo de vuelta). La tecla
`3` abre/cierra la **ventana OpenGL nativa** (`glview`) con la escena 3D de la vista
activa: el modo es global y persiste al cambiar de vista, la TUI sigue en 2D.
"""

from __future__ import annotations

import asyncio
import math
import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static
from rich.text import Text

from .bigfont import render_big, render_heart
from .config import Config, save_config
from .gauges import Gauge
from .glview import GLHost
from .history import Session
from .laps import LapTracker
from .track import Track
from .export import export_session
from .zones import zone_for_hr, power_band, W_COLORS, POWER_BANDS

SPD_COLOR = "#22d3ee"
CAD_COLOR = "#a78bfa"
GLYPH8 = " ▁▂▃▅▆▇█"
# orden de las vistas para navegar con las flechas
VIEWS = ["hr", "watts", "gauge", "pista"]
VIEW_KEYS = {"hr": "h", "watts": "w", "gauge": "g", "pista": "p"}
VIEW_NAMES = {"hr": "FC", "watts": "watts", "gauge": "tablero", "pista": "pista"}
# presets del largo de vuelta para la vista pista (se recorren con [ y ])
LAP_PRESETS = [1000, 2000, 3000, 5000, 10000, 20000]


def hints_text(view: str, note: str = "") -> Text:
    """Linea de ayuda: la vista activa va en negrita; `note` avisa de la ventana 3D."""
    t = Text(" q: salir y grabar FIT+CSV · x: reiniciar · s: reconectar · c: config · ")
    t.append("← →: ", style="#6b7280")
    for i, v in enumerate(VIEWS):
        if i:
            t.append(" ", style="#6b7280")
        if v == view:
            t.append(f"{VIEW_KEYS[v]} {VIEW_NAMES[v]}", style="bold #f8fafc")
        else:
            t.append(VIEW_KEYS[v], style="#9ca3af")
    t.append(" · 3: dashboard · r: race (rueda: zoom) · [ ]: largo vuelta", style="#6b7280")
    if note:
        t.append(f" · {note}", style="bold #f97316")
    return t


def _round_up(v: float, m: int) -> float:
    return math.ceil(v / m) * m


def _g8(level: float) -> str:
    """Glifo de 8 niveles con indice siempre valido (0..7), tolerante a inf/nan."""
    try:
        v = max(0, min(7, int(round(level))))
    except (OverflowError, ValueError, TypeError):
        v = 7 if level == float("inf") else 0
    return GLYPH8[v]


def fmt_clock(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class ConfigModal(ModalScreen):
    """Cambiar edad / sexo / peso / FTP sin reiniciar la sesion."""

    CSS = """
    #cfgbox { width: 50; height: auto; padding: 1 2; background: $panel; border: round #ef4444; }
    Label { color: $text-muted; }
    Input { margin: 0 0 1 0; }
    #err { color: #ef4444; height: 1; }
    """

    FIELDS = (("in_age", "Edad"), ("in_sex", "Sexo (M/F)"),
              ("in_weight", "Peso (kg)"), ("in_ftp", "FTP (W)"))

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        with Vertical(id="cfgbox"):
            for _id, lab in self.FIELDS:
                yield Label(lab)
                yield Input(id=_id)
            yield Static("", id="err")
            with Horizontal():
                yield Button("Guardar", id="ok", variant="success")
                yield Button("Cancelar", id="no", variant="default")

    def on_mount(self) -> None:
        vals = {"in_age": str(self.cfg.age), "in_sex": self.cfg.sex,
                "in_weight": str(self.cfg.weight), "in_ftp": str(self.cfg.ftp)}
        for k, v in vals.items():
            self.query_one(f"#{k}", Input).value = v

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "no":
            self.dismiss(None)
            return
        err = self.query_one("#err", Static)
        try:
            age = int(self.query_one("#in_age", Input).value)
            sex = self.query_one("#in_sex", Input).value.strip().upper()[:1]
            weight = float(self.query_one("#in_weight", Input).value)
            ftp = int(self.query_one("#in_ftp", Input).value)
            assert 5 <= age <= 120 and sex in ("M", "F") and 20 <= weight <= 400 \
                and 50 <= ftp <= 600
        except Exception:  # noqa: BLE001
            err.update("Valores invalidos (edad 5-120, M/F, peso 20-400, FTP 50-600)")
            return
        self.cfg.age, self.cfg.sex = age, sex
        self.cfg.weight, self.cfg.ftp = weight, ftp
        self.dismiss(self.cfg)


class HRDash(App):
    TITLE = "HRDash"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("q", "quit_session", "Salir y grabar"),
        ("x", "reset_session", "Reiniciar"),
        ("s", "reconnect", "Reconectar"),
        ("c", "open_config", "Config"),
        ("h", "view_hr", "Vista FC"),
        ("w", "view_w", "Vista watts"),
        ("g", "view_gauge", "Tablero"),
        ("p", "view_pista", "Vista pista"),
        ("3", "toggle_gl_dash", "Dashboard 3D on/off"),
        ("r", "toggle_gl_race", "Race 3D on/off"),
        ("left", "cycle_view(-1)", "Vista anterior"),
        ("right", "cycle_view(1)", "Vista siguiente"),
        ("[", "cycle_lap(-1)", "Vuelta mas corta"),
        ("]", "cycle_lap(1)", "Vuelta mas larga"),
    ]

    CSS = """
    Screen { padding: 0 1; }
    #status { height: 1; color: #d1d5db; }
    #tiles { height: 5; }
    #t_heart { width: 13; height: 1fr; }
    .tile { width: 1fr; height: 1fr; }
    #ch_hr { height: 1fr; min-height: 4; }
    #ch_w  { height: 5; }
    #ch_vc { height: 2; }
    #stats { height: 4; }
    #zonesrow { height: 7; }
    #zonesrow > Static { width: 1fr; }
    #zones { margin-right: 2; }
    #hints { height: 1; color: #6b7280; }

    /* vista pista: el ovalo ocupa el lugar de tiles/graficos/zonas */
    #pista { height: 1fr; min-height: 6; display: none; }
    #lapboard { height: 7; display: none; }
    Screen.pista #pista, Screen.pista #lapboard { display: block; }
    Screen.pista #tiles,
    Screen.pista #ch_hr,
    Screen.pista #ch_w,
    Screen.pista #ch_vc,
    Screen.pista #stats,
    Screen.pista #zonesrow { display: none; }

    /* vista tablero: cluster de gauges ocupa el lugar de todo lo demas */
    #cluster { height: 1fr; display: none; }
    #crow1 { height: 3fr; }
    #crow2 { height: 2fr; }
    .gauge { width: 1fr; height: 1fr; }
    #g_hr { width: 2fr; }
    #g_w { width: 1fr; }
    #g_panel { width: 2fr; padding: 0 1; }
    Screen.tach #cluster { display: block; }
    Screen.tach #tiles,
    Screen.tach #ch_hr,
    Screen.tach #ch_w,
    Screen.tach #ch_vc,
    Screen.tach #stats,
    Screen.tach #zonesrow { display: none; }
    """

    def __init__(self, hr_source, trainer_source, config: Config):
        super().__init__()
        self.hr = hr_source
        self.tr = trainer_source
        self.config = config
        self.session = Session(config)
        self._stop = asyncio.Event()
        self.export_result: dict | None = None
        self._ui_error = ""
        self._err_round = False
        self._pulse = 0
        self.view = "hr"          # "hr" | "watts" | "gauge" | "pista": vista principal
        self.gl = False           # True: ventana OpenGL 3D abierta
        self.gl_views: list[str] = []   # vistas 3D activas: "gauge" (dashboard) y "pista" (race)
        self._glhost: GLHost | None = None
        self._gl_msg = ""         # aviso de la ventana 3D (cerrada / sin dependencias)
        self._beat_t0 = time.time()
        self._beat_period = 1.0
        self._heart_lvl = -1      # ultimo nivel dibujado (evita redibujos)
        self._last_rescue = 0.0
        self._last_autosave = time.time()
        self._gauges = {"g_hr": Gauge(), "g_w": Gauge(),
                        "g_v": Gauge(), "g_c": Gauge()}
        self._needle = {"hr": 0.0, "w": 0.0, "v": 0.0, "c": 0.0}
        self.laps = LapTracker(config.lap_m, config)
        self._track = Track()
        self._marker = 0.0         # posicion suavizada de la bici (0..1 de la vuelta)
        self._trail: list[float] = []
        self._lap_flash = 0.0      # instante de la ultima vuelta cerrada

    # ---- layout ----
    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="tiles"):
            yield Static(id="t_heart")
            yield Static(id="t_hr", classes="tile")
            yield Static(id="t_w", classes="tile")
            yield Static(id="t_v", classes="tile")
            yield Static(id="t_c", classes="tile")
        yield Static(id="ch_hr")
        yield Static(id="ch_w")
        yield Static(id="ch_vc")
        yield Static(id="stats")
        with Horizontal(id="zonesrow"):
            yield Static(id="zones")
            yield Static(id="zones_w")
        yield Static(id="pista")
        yield Static(id="lapboard")
        with Vertical(id="cluster"):
            with Horizontal(id="crow1"):
                yield Static(id="g_hr", classes="gauge")
                yield Static(id="g_w", classes="gauge")
            with Horizontal(id="crow2"):
                yield Static(id="g_v", classes="gauge")
                yield Static(id="g_c", classes="gauge")
                yield Static(id="g_panel")
        yield Static(id="hints")

    def on_mount(self) -> None:
        self.run_worker(self._run(self.hr, "banda"), group="hr", exit_on_error=False)
        if self.tr is not None:
            self.run_worker(self._run(self.tr, "rodillo"), group="trainer",
                            exit_on_error=False)
        self.set_interval(1.0, self.tick)
        self.set_interval(0.1, self._tick_heart)   # corazon a 10 fps, independiente del dato
        self.set_interval(0.1, self._tick_gauges)  # agujas a 10 fps (solo en vista tablero)
        self.set_interval(0.1, self._tick_pista)   # marcador de la bici a 10 fps
        self.query_one("#hints", Static).update(hints_text(self.view))
        self.refresh_all()

    async def _run(self, source, label: str) -> None:
        try:
            await source.run(self._stop)
        except Exception as exc:  # noqa: BLE001
            source.error = f"{label}: {exc}"

    # ---- acciones ----
    def action_reset_session(self) -> None:
        self.session.reset()
        self.laps.reset()
        self._trail.clear()
        self._marker = 0.0
        self._lap_flash = 0.0

    def action_reconnect(self) -> None:
        for src in (self.hr, self.tr):
            if src is not None and hasattr(src, "request_reconnect"):
                src.request_reconnect()

    def action_view_hr(self) -> None:
        self._set_view("hr")

    def action_view_w(self) -> None:
        self._set_view("watts")

    def action_view_gauge(self) -> None:
        self._set_view("gauge")

    def action_view_pista(self) -> None:
        self._set_view("pista")

    def action_cycle_view(self, delta: int) -> None:
        """Recorre las vistas con ← →: FC -> watts -> tablero -> pista."""
        if len(self.screen_stack) > 1:
            return          # hay modal abierto: ahi las flechas mueven el cursor
        i = VIEWS.index(self.view) if self.view in VIEWS else 0
        self._set_view(VIEWS[(i + delta) % len(VIEWS)])

    def action_cycle_lap(self, delta: int) -> None:
        """Recorre los presets de largo de vuelta (1, 2, 3, 5, 10, 20 km)."""
        try:
            i = LAP_PRESETS.index(self.config.lap_m)
        except ValueError:
            LAP_PRESETS.append(int(self.config.lap_m))
            i = LAP_PRESETS.index(int(self.config.lap_m))
        self._set_lap_m(LAP_PRESETS[(i + delta) % len(LAP_PRESETS)])

    def _set_lap_m(self, lap_m: int) -> None:
        lap_m = int(max(100, lap_m))
        if lap_m == self.config.lap_m:
            return
        self.config.lap_m = lap_m
        self.laps.lap_m = lap_m
        self.laps.recompute(self.session.samples)   # rehace las vueltas con el nuevo largo
        self._trail.clear()
        self._marker = self.laps.frac
        try:
            save_config(self.config)
        except Exception:  # noqa: BLE001
            pass
        self.refresh_all()

    def _set_view(self, v: str) -> None:
        self.view = v
        for cls in ("tach", "pista"):
            self.screen.remove_class(cls)
        if v == "gauge":
            self.screen.add_class("tach")
        elif v == "pista":
            self.screen.add_class("pista")
        self._tick_gauges()      # primera pincelada inmediata (o vuelve el grafico)
        self._tick_pista()
        if not self._ui_error:
            self._hints()
        self._push_gl()          # la ventana muestra la escena de la vista recién activada
        self.refresh_all()

    def action_open_config(self) -> None:
        self.push_screen(ConfigModal(self.config), self._config_updated)

    def _config_updated(self, cfg: Config | None) -> None:
        if cfg is not None:
            save_config(cfg)
            self.refresh_all()

    def action_quit_session(self) -> None:
        self._kill_gl()                  # la ventana 3D se va con el dashboard
        try:
            name = getattr(self.hr, "name", "HRDash")
            if getattr(self.tr, "name", None):
                name += f" + {self.tr.name}"
            self.export_result = export_session(self.session, self.config, name)
        except Exception as exc:  # noqa: BLE001
            self.export_result = {"fit_error": str(exc)}
        self._stop.set()
        self.exit()

    # ---- ventana 3D (OpenGL nativo, en proceso aparte) ----
    def action_toggle_gl_dash(self) -> None:
        """`3`: abre o cierra la vista dashboard (cluster) en la ventana 3D."""
        self._toggle_gl_view("gauge")

    def action_toggle_gl_race(self) -> None:
        """`r`: abre o cierra la vista race (pista, camara chase) en la ventana 3D."""
        self._toggle_gl_view("pista")

    def _toggle_gl_view(self, v: str) -> None:
        """Toggle independiente por vista: con dos vistas, split o dos ventanas."""
        if v in self.gl_views:
            self.gl_views.remove(v)
            if not self.gl_views:
                self._gl_msg = "ventana 3D cerrada"
                self._kill_gl()
            else:
                self._push_gl()
        else:
            if not self.gl_views:
                host = self._glhost or GLHost()
                if not host.start():
                    self._glhost = None
                    self._gl_msg = host.error or "3D no disponible: pip install -e '.[gl]'"
                    self._hints()
                    return
                self._glhost = host
                self.gl = True
                self._gl_msg = ""
            self.gl_views.append(v)
            self._push_gl()
        self._hints()

    def _gl_snapshot(self) -> dict:
        """Estado de la sesion para el proceso de la ventana (mismo dato que la TUI)."""
        s, cfg, tr = self.session, self.config, self.tr
        hr = int(s.current_hr or (getattr(self.hr, "latest_hr", None) or 0))
        w = int(s.current_watts or ((getattr(tr, "watts", None) or 0) if tr else 0))
        cad = float(s.current_cadence or ((getattr(tr, "cadence", None) or 0.0) if tr else 0.0))
        spd = float(s.current_speed or ((getattr(tr, "speed_kmh", None) or 0.0) if tr else 0.0))
        zones = cfg.zones()
        zone = zone_for_hr(hr or 60, zones, cfg.computed_hrmax())
        band = power_band(w, cfg.ftp)
        hrmax = int(cfg.computed_hrmax())
        return {
            "view": self.view,
            "views": list(self.gl_views),
            "hr": hr, "watts": w, "cadence": cad, "speed": spd,
            "hrmax": hrmax, "ftp": int(cfg.ftp),
            "zone_idx": zone.idx, "zone_name": zone.name, "zone_color": zone.color,
            "wband": band, "wzone_color": W_COLORS[band],
            "alarm": bool(hr and hr >= 0.95 * hrmax),
            "max_hr": int(s.max_hr), "avg_hr": float(s.avg_hr), "pct_max": float(s.pct_max),
            "max_watts": int(s.max_watts), "avg_watts": float(s.avg_watts),
            "max_speed": float(s.max_speed), "avg_speed": float(s.avg_speed),
            "max_cadence": float(s.max_cadence), "avg_cadence": float(s.avg_cadence),
            "elapsed_s": int(s.elapsed_s), "total_kcal": float(s.total_kcal),
            "dist_km": float(s.dist_km), "n": int(s.n),
            "zone_secs": int(s.zone_seconds[zone.idx]),
            "kcal_min": 60.0 * s.total_kcal / max(1, s.elapsed_s) if s.n else 0.0,
            "wkg": float(s.wkg),
            "lap_km": float(self.laps.lap_m / 1000.0),
            "age": int(cfg.age), "weight": float(cfg.weight), "sex": cfg.sex,
            "samples": [(x.hr, x.watts or 0, x.zone_idx) for x in s.window(70)],
            "pzone_kj": [float(x) for x in s.pzone_kj],
            "pzone_s": [int(x) for x in s.pzone_seconds],
            "zones": [(float(z.low_pct), min(float(z.high_pct), 100.0), z.color) for z in zones],
            "bands": [(float(lo), min(float(hi), 900.0), c)
                      for (_nombre, lo, hi), c in zip(POWER_BANDS, W_COLORS)],
            "zcolors": [z.color for z in zones],
            "wcolors": list(W_COLORS),
            "lap_m": int(self.laps.lap_m), "lap_frac": float(self.laps.frac),
        }

    def _push_gl(self) -> None:
        if not (self.gl and self._glhost is not None):
            return
        try:
            self._glhost.push(self._gl_snapshot())
        except Exception as exc:  # noqa: BLE001 - la ventana nunca interrumpe el muestreo
            self._gl_msg = f"3D: {type(exc).__name__}: {exc}"[:80]
            self._kill_gl()
            self._hints()

    def _check_gl(self) -> None:
        """Si la ventana murio (cierre del usuario o fallo GL), se vuelve a 2D solos."""
        if not (self.gl and self._glhost is not None):
            return
        err = self._glhost.poll_error()
        if err:
            self._gl_msg = err
            self._kill_gl()
            self._hints()
        elif not self._glhost.alive():
            self._gl_msg = "ventana 3D cerrada"
            self._kill_gl()
            self._hints()

    def _kill_gl(self) -> None:
        self.gl = False
        self.gl_views = []
        if self._glhost is not None:
            self._glhost.stop()
            self._glhost = None

    def _hints(self) -> None:
        try:
            self.query_one("#hints", Static).update(hints_text(self.view, self._gl_msg))
        except Exception:  # noqa: BLE001 - la linea de ayuda no debe tumbar la app
            pass

    # ---- ciclo de refresco (1 Hz) ----
    def tick(self) -> None:
        try:
            hr = getattr(self.hr, "latest_hr", None)
            if hr is not None:
                tr = self.tr
                s = self.session.add(
                    hr,
                    watts=getattr(tr, "watts", None) if tr else None,
                    cadence=getattr(tr, "cadence", None) if tr else None,
                    speed_kmh=getattr(tr, "speed_kmh", None) if tr else None,
                    dist_m=getattr(tr, "dist_m", None) if tr else None,
                )
                if self.laps.add(s):
                    self._lap_flash = time.time()
        except Exception as exc:  # noqa: BLE001 - el muestreo no debe morir jamas
            self._show_error(f"tick: {type(exc).__name__}: {exc}")
        self.refresh_all()
        self._autosave()

    # ---- robustez: secciones de dibujo aisladas + guardado de rescate ----
    def _safe_draw(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            self._err_round = True
            msg = f"{getattr(fn, '__name__', fn)}: {type(exc).__name__}: {exc}"
            self._show_error(msg)
            self._rescue_export()

    def _show_error(self, msg: str) -> None:
        self._ui_error = msg
        try:
            self.query_one("#hints", Static).update(
                Text(f" ⚠ {msg[:100]}  — los datos se siguen grabando "
                     "(RESCATE/AUTO en exportes/)", style="bold #ef4444"))
        except Exception:  # noqa: BLE001
            pass

    def _rescue_export(self) -> None:
        now = time.time()
        if now - self._last_rescue < 90 or self.session.n == 0:
            return
        self._last_rescue = now
        try:
            export_session(self.session, self.config, self.hr.name, suffix="_RESCATE")
        except Exception:  # noqa: BLE001 - no hay segundo intento de rescate
            pass

    def _autosave(self) -> None:
        """Autoguardado cada 300 s para no depender de la salida limpia."""
        now = time.time()
        if now - self._last_autosave < 300 or self.session.n == 0:
            return
        self._last_autosave = now
        try:
            export_session(self.session, self.config, self.hr.name, suffix="_AUTO")
        except Exception:  # noqa: BLE001
            pass

    # ---- dibujado ----
    def _sensor_badge(self, src, label) -> list:
        if src is None:
            return [Text(f" · {label} off", style="#6b7280")]
        ok = getattr(src, "connected", False)
        col = "#22c55e" if ok else "#ef4444"
        err = getattr(src, "error", "")
        txt = src.name if ok else (err or "sin senal")
        return [Text(" · ", style="#6b7280"),
                Text("●" if ok else "○", style=f"bold {col}"),
                Text(f" {label} {txt}", style=col if not ok else "#d1d5db")]

    def refresh_all(self) -> None:
        s, cfg = self.session, self.config
        self._err_round = False
        self._check_gl()
        hr = s.current_hr if s.n else (getattr(self.hr, "latest_hr", None) or 0)
        zone = zone_for_hr(hr or 60, cfg.zones(), cfg.computed_hrmax())
        self._safe_draw(self._draw_status, zone)
        if self.view == "gauge":
            self._safe_draw(self._draw_cluster)
        elif self.view == "pista":
            self._safe_draw(self._draw_pista, hr, zone)
            self._safe_draw(self._draw_laps, hr, zone)
        else:
            self._safe_draw(self._draw_tiles, hr, zone)
            self._safe_draw(self._draw_charts, zone)
            self._safe_draw(self._draw_stats)
            self._safe_draw(self._draw_zones)
            self._safe_draw(self._draw_zones_w)
        self._push_gl()                  # 1 Hz: la ventana 3D recibe el estado nuevo
        if self._err_round:
            return                       # _show_error ya pinto la alerta
        if self._ui_error:               # ronda limpia tras un error: restaurar ayuda
            self._ui_error = ""
            self._hints()

    def _draw_status(self, zone) -> None:
        src = self.hr
        ok = getattr(src, "connected", False)
        parts = [Text("● " if ok else "○ ", style=f"bold {zone.color if ok else '#ef4444'}"),
                 Text(f"{src.name}", style="bold white"),
                 Text("  " + ("conectada" if ok else "BANDA DESCONECTADA"),
                      style="#22c55e" if ok else "bold #ef4444")]
        bat = getattr(src, "battery", None)
        if bat is not None:
            parts.append(Text(f" · pila {bat}%", style="#9ca3af"))
        contact = getattr(src, "contact", None)
        if contact is not None:
            parts.append(Text(f" · contacto {'si' if contact else 'NO'}",
                              style="#9ca3af" if contact else "#f97316"))
        parts += self._sensor_badge(self.tr, "rodillo")
        parts.append(Text(f" · ⏱ {fmt_clock(self.session.elapsed_s)}", style="#9ca3af"))
        parts.append(Text(f" · Z{zone.idx} {zone.name}", style=f"bold {zone.color}"))
        self.query_one("#status", Static).update(Text.assemble(*parts))

    def _tile(self, widget_id: str, value: str, unit: str, sub: str, color: str) -> None:
        w = self.query_one(f"#{widget_id}", Static)
        compact = (w.size.width or 40) < 34
        if compact:
            t = Text(justify="center")
            t.append(f"{value:>5} {unit}\n", style=f"b {color}")
            t.append(f" {sub}", style="#9ca3af")
            w.update(t)
            return
        rows = render_big(value)
        t = Text()
        for i, r in enumerate(rows):
            t.append(r.center((w.size.width or 34) - 2), style=f"b {color}")
            t.append("\n")
        t.append(f"{unit} · {sub}", style="#9ca3af")
        w.update(t)

    PULSE_LEVELS = (1, 3, 2, 0)   # (legado) ciclo historico de 1 Hz

    def _tick_heart(self) -> None:
        """Animacion del corazon: su propio timer a 10 fps, no al 1 Hz del dato.

        Frecuencia del latido = FC actual (lub-dub con periodo 60/FC), amplitud
        con decaimiento exponencial: suba violenta, bajada suave.
        """
        try:
            hr = self.session.current_hr or (getattr(self.hr, "latest_hr", None) or 0)
            if self.view == "gauge":
                return                     # el corazon esta oculto: no gastar tramas
            if not hr:
                lvl, color = 0, "#4b5563"        # en reposo: gris pequeno
            else:
                now = time.time()
                self._beat_period = max(0.34, min(1.4, 60.0 / hr))
                if now - self._beat_t0 >= self._beat_period:
                    self._beat_t0 = now
                t = now - self._beat_t0
                lub = math.exp(-t * 8.0 / self._beat_period)
                d0 = 0.30 * self._beat_period
                dub = 0.55 * math.exp(-max(0.0, t - d0) * 8.0 / self._beat_period) \
                    if t >= d0 else 0.0
                amp = min(1.0, max(lub, dub))
                lvl = int(round(amp * 3))
                color = "#ff5e8a" if amp > 0.55 else "#ff2d55"
            if lvl != self._heart_lvl:
                self._heart_lvl = lvl
                self._render_heart(lvl, color)
        except Exception as exc:  # noqa: BLE001 - la animacion jamas debe tumbar la app
            self._show_error(f"heart: {type(exc).__name__}: {exc}")

    def _render_heart(self, level: int, color: str) -> None:
        hw = self.query_one("#t_heart", Static)
        w = hw.size.width or 13
        t = Text()
        rows = render_heart(level)
        for i, r in enumerate(rows):
            t.append(r.center(w), style=f"bold {color}")
            if i < len(rows) - 1:
                t.append("\n")
        hw.update(t)

    def _draw_tiles(self, hr: int, zone) -> None:
        s, cfg = self.session, self.config
        w_now = s.current_watts or (getattr(self.tr, "watts", None) or 0)
        spd = s.current_speed or (getattr(self.tr, "speed_kmh", None) or 0.0)
        cad = s.current_cadence or (getattr(self.tr, "cadence", None) or 0.0)
        pct_ftp = 100.0 * w_now / cfg.ftp if cfg.ftp else 0.0
        self._tile("t_hr", str(hr) if hr else "--", "FC bpm",
                   f"Z{zone.idx} {zone.name}", zone.color)
        self._tile("t_w", str(w_now) if w_now else "--", "WATTS",
                   f"{pct_ftp:.0f}%FTP {s.wkg:.1f}W/kg",
                   W_COLORS[power_band(w_now, cfg.ftp)])
        self._tile("t_v", str(int(spd)) if spd else "--", "km/h",
                   f"dec {spd - int(spd):.1f} media {s.avg_speed:.1f}" if spd else "sin rodillo",
                   SPD_COLOR)
        self._tile("t_c", str(int(cad)) if cad else "--", "rpm",
                   f"media {s.avg_cadence:.0f}" if cad else "sin rodillo",
                   CAD_COLOR)

    def _bars(self, samples, rows, width, value_fn, color_fn, floor, top,
              blank=" ", prefix=""):
        """Texto con `rows` lineas de barras verticales (1 char/muestra)."""
        cols = []
        for s in samples:
            v = value_fn(s)
            if v is None or v <= 0:
                cols.append(0)
            else:
                h = int(round((v - floor) / max(1e-6, top - floor) * rows))
                cols.append(max(1, min(rows, h)))
        colors = [color_fn(s) for s in samples]
        t = Text()
        for r in range(rows, 0, -1):
            if prefix and r == rows:
                t.append(prefix, style="#6b7280")
            for i in range(width):
                if i < len(cols):
                    if cols[i] >= r:
                        t.append("█", style=colors[i])
                    else:
                        t.append("▏" if r == 1 else blank)
                else:
                    t.append(blank)
            t.append("\n")
        return t

    def _hr_chart(self, samples, rows, width):
        cfg = self.config
        hr_top = max(cfg.computed_hrmax(), max((x.hr for x in samples), default=0) + 5)
        zones = cfg.zones()
        return self._bars(samples, rows, width,
                          lambda x: x.hr, lambda x: zones[min(x.zone_idx, 5)].color,
                          40, hr_top, prefix=f"HR 40-{hr_top}  ")

    def _w_chart(self, samples, rows, width):
        cfg = self.config
        w_top = max(int(cfg.ftp * 1.25),
                    max(((x.watts or 0) for x in samples), default=0) + 30, 100)
        return self._bars(samples, rows, width, lambda x: x.watts or 0,
                          lambda x: W_COLORS[power_band(x.watts or 0, cfg.ftp)],
                          0, w_top, prefix=f"W 0-{w_top}    ")

    def _draw_charts(self, zone) -> None:
        s = self.session
        ch_main = self.query_one("#ch_hr", Static)   # area grande: vista activa
        ch_strip = self.query_one("#ch_w", Static)   # tira chica: la otra metrica
        width = max(20, (ch_main.size.width or 80) - 9)
        samples = s.window(width)
        main_rows = max(3, ch_main.size.height or 6)

        if self.view == "watts":
            ch_main.update(self._w_chart(samples, main_rows, width))
            ch_strip.update(self._hr_chart(samples, 4, width))
        else:
            ch_main.update(self._hr_chart(samples, main_rows, width))
            ch_strip.update(self._w_chart(samples, 4, width))

        # velocidad y cadencia: 1 fila de glifos de 8 niveles cada una
        vc = self.query_one("#ch_vc", Static)
        v_top = max(40.0, max(((x.speed_kmh or 0) for x in samples), default=0) + 2)
        c_top = max(140.0, max(((x.cadence or 0) for x in samples), default=0) + 15)
        line_v, line_c = Text("VEL "), Text("CAD ")
        for x in samples[-width:]:
            lv = (x.speed_kmh or 0) / v_top * 7 if x.speed_kmh else 0
            lc = (x.cadence or 0) / c_top * 7 if x.cadence else 0
            line_v.append(_g8(lv), style=SPD_COLOR if lv else "#374151")
            line_c.append(_g8(lc), style=CAD_COLOR if lc else "#374151")
        pad = width - len(samples[-width:])
        line_v.append(" " * max(0, pad))
        line_c.append(" " * max(0, pad))
        vc.update(Text.assemble(line_v, "\n", line_c, "\n"))

    def _draw_stats(self) -> None:
        s, cfg = self.session, self.config
        n = s.n
        t = Text()

        def row(pairs) -> None:
            for label, value, style in pairs:
                t.append(f" {label} ", style="#6b7280")
                t.append(f"{value} ", style=style)
                t.append("│", style="#374151")
            t.append("\n")

        row([("FC MAX", f"{s.max_hr or '--'}", "b #ef4444"),
             ("FC MED", f"{s.avg_hr:.0f}" if n else "--", "b #facc15"),
             ("FC MIN", f"{s.min_hr or '--'}", "b #2f7bff"),
             ("%FCmáx", f"{s.pct_max:.0f}%" if n else "--", "white"),
             ("FCmáx est", str(cfg.computed_hrmax()), "#9ca3af"),
             ("EDAD/PESO", f"{cfg.age}a/{cfg.weight:.0f}kg {cfg.sex}", "#9ca3af")])
        row([("W MAX", f"{s.max_watts or '--'}", "b #f97316"),
             ("W MED", f"{s.avg_watts:.0f}" if s.avg_watts else "--", "b #facc15"),
             ("W:KG", f"{s.wkg:.2f}" if s.current_watts else "--", "#9ca3af"),
             ("FTP", f"{cfg.ftp}W", "b #22c55e"),
             ("DIST", f"{s.dist_km:.2f} km", "b " + SPD_COLOR),
             ("VEL MED", f"{s.avg_speed:.1f}", SPD_COLOR),
             ("VEL MÁX", f"{s.max_speed:.1f}", SPD_COLOR)])
        row([("CAD MED", f"{s.avg_cadence:.0f}" if s.avg_cadence else "--", CAD_COLOR),
             ("CAD MÁX", f"{s.max_cadence:.0f}" if s.max_cadence else "--", CAD_COLOR),
             ("MUESTRAS", str(n), "#9ca3af"),
             ("TIEMPO", fmt_clock(s.elapsed_s), "white"),
             ("KCAL", f"{s.total_kcal:.0f}", "b #22c55e"),
             ("KCAL/MIN", f"{s.total_kcal / max(1, s.elapsed_s) * 60:.1f}" if n else "0.0", "#9ca3af")])
        if self.tr is not None and not getattr(self.tr, "connected", False):
            t.append(" sin telemetria del rodillo: "
                     f"{getattr(self.tr, 'error', '') or 'esperando señales (pedalee?)'}",
                     style="italic #f97316")
        self.query_one("#stats", Static).update(t)

    def _draw_zones(self) -> None:
        s = self.session
        zones = self.config.zones()
        w = self.query_one("#zones", Static).size.width or 70
        barw = max(8, min(28, w - 40))
        t = Text()
        t.append(f" ZONAS FC (%FCmax {self.config.computed_hrmax()})\n", style="bold #9ca3af")
        total = max(1, sum(s.zone_seconds))
        for z in zones:
            secs = s.zone_seconds[z.idx]
            kcal = s.zone_kcal[z.idx]
            fill = int(round(barw * secs / total)) if secs else 0
            t.append(f" {z.key} ", style=f"bold {z.color}")
            t.append(f"{z.name:<12}"[:12], style=z.color)
            t.append("│" + "█" * fill, style=z.color)
            t.append("░" * (barw - fill), style="#374151")
            t.append(f"│ {fmt_clock(secs):>8}  {kcal:5.1f} kcal\n", style="#9ca3af")
        self.query_one("#zones", Static).update(t)

    def _draw_zones_w(self) -> None:
        """Panel derecho: zonas de potencia por %FTP (mismas bandas del grafico W)."""
        s = self.session
        cfg = self.config
        w = self.query_one("#zones_w", Static).size.width or 70
        barw = max(6, min(26, w - 42))
        t = Text()
        t.append(f" ZONAS POTENCIA (FTP {cfg.ftp} W)\n", style="bold #9ca3af")
        total = max(1, sum(s.pzone_seconds))
        for i, (name, lo, hi) in enumerate(POWER_BANDS):
            secs = s.pzone_seconds[i]
            kj = s.pzone_kj[i]
            col = W_COLORS[i]
            fill = int(round(barw * secs / total)) if secs else 0
            rng = f"{lo:.0f}-{hi:.0f}%" if hi < 900 else f"> {lo:.0f}%"
            t.append(f" {name:<9}", style=f"bold {col}")
            t.append(f" {rng:>9} ", style="#6b7280")
            t.append("│" + "█" * fill, style=col)
            t.append("░" * (barw - fill), style="#374151")
            t.append(f"│ {fmt_clock(secs):>7} {kj:6.0f} kJ\n", style="#9ca3af")
        self.query_one("#zones_w", Static).update(t)

    # ---- vista tablero: cluster de gauges con agujas animadas ----
    def _needle_targets(self) -> dict:
        s, tr = self.session, self.tr
        hr = s.current_hr or (getattr(self.hr, "latest_hr", None) or 0)
        w = s.current_watts or (getattr(tr, "watts", None) if tr else 0) or 0
        v = s.current_speed or (getattr(tr, "speed_kmh", None) if tr else 0) or 0
        c = s.current_cadence or (getattr(tr, "cadence", None) if tr else 0) or 0
        return {"hr": float(hr), "w": float(w), "v": float(v), "c": float(c)}

    def _tick_gauges(self) -> None:
        """Inercia de agujas a 10 fps: suben rapido (~0.5 s), bajan suave (~1 s)."""
        if self.view != "gauge":
            return
        try:
            for key, target in self._needle_targets().items():
                d = self._needle[key]
                d += (target - d) * (0.5 if target > d else 0.14)
                if abs(target - d) < 0.05:
                    d = target
                self._needle[key] = max(0.0, d)
            self._safe_draw(self._draw_cluster)
        except Exception as exc:  # noqa: BLE001 - la animacion jamas debe tumbar la app
            self._show_error(f"gauges: {type(exc).__name__}: {exc}")

    def _hr_scale(self):
        cfg = self.config
        lo, hrmax = 40.0, max(1, cfg.computed_hrmax())
        hi = _round_up(max(hrmax + 10, self.session.max_hr + 10), 10)
        segs = []
        for z in cfg.zones():
            f0 = max(0.0, min(1.0, (z.low_pct / 100 * hrmax - lo) / (hi - lo)))
            f1 = max(0.0, min(1.0, (min(z.high_pct, 100.0) / 100 * hrmax - lo) / (hi - lo)))
            if f1 > f0:
                segs.append((f0, f1, z.color))
        return lo, hi, segs

    def _w_scale(self):
        cfg, s = self.config, self.session
        hi = _round_up(max(cfg.ftp * 1.25, s.max_watts + 25, 100), 25)
        segs = []
        for (name, p0, p1), col in zip(POWER_BANDS, W_COLORS):
            f0 = max(0.0, min(1.0, p0 / 100 * cfg.ftp / hi))
            f1 = max(0.0, min(1.0, p1 / 100 * cfg.ftp / hi))
            if f1 > f0:
                segs.append((f0, f1, col))
        return 0.0, float(hi), segs

    def _render_gauge(self, wid: str, value, lo, hi, segs, major, minor,
                      label, sub, color, big=False, avg=None) -> None:
        st = self.query_one(f"#{wid}", Static)
        g = self._gauges[wid]
        st.update(g.render(st.size.width or 40, st.size.height or 16, value,
                           lo, hi, segs, major, minor, label, sub, color,
                           big=big, avg=avg))

    def _draw_cluster(self) -> None:
        s, cfg = self.session, self.config
        nd = self._needle
        zone = zone_for_hr(int(nd["hr"]) or 60, cfg.zones(), cfg.computed_hrmax())

        lo, hi, segs = self._hr_scale()
        self._render_gauge("g_hr", nd["hr"], lo, hi, segs, 20, 10,
                           str(int(round(nd["hr"]))) if nd["hr"] else "--",
                           f"bpm · Z{zone.idx} {zone.name}", zone.color,
                           big=True, avg=s.avg_hr or None)

        lo, hi, segs = self._w_scale()
        pct = 100.0 * nd["w"] / cfg.ftp if cfg.ftp else 0.0
        major = 50 if hi >= 200 else 25
        self._render_gauge("g_w", nd["w"], lo, hi, segs, major, major // 2,
                           f"{int(round(nd['w']))} W" if nd["w"] else "-- W",
                           f"{pct:.0f}%FTP · {s.wkg:.1f}W/kg",
                           W_COLORS[power_band(int(nd["w"]), cfg.ftp)],
                           avg=s.avg_watts or None)

        lo, hi = 0.0, _round_up(max(40.0, s.max_speed + 5), 10)
        self._render_gauge("g_v", nd["v"], lo, hi, [(0.0, 1.0, SPD_COLOR)],
                           10, 5, f"{nd['v']:.1f} km/h" if nd["v"] else "-- km/h",
                           f"media {s.avg_speed:.1f}", SPD_COLOR,
                           avg=s.avg_speed or None)

        hi = 160.0 if s.max_cadence > 130 else 140.0
        self._render_gauge("g_c", nd["c"], 0.0, hi, [(0.0, 1.0, CAD_COLOR)],
                           20, 10, f"{int(round(nd['c']))} rpm" if nd["c"] else "-- rpm",
                           f"media {s.avg_cadence:.0f}", CAD_COLOR,
                           avg=s.avg_cadence or None)

        self._draw_panel()

    def _draw_panel(self) -> None:
        """Panel digital del tablero: resumen de sesion en formato panel de a bordo."""
        s, cfg = self.session, self.config
        t = Text()

        def row(pairs) -> None:
            for label, value, style in pairs:
                t.append(f" {label} ", style="#6b7280")
                t.append(f"{value} ", style=style)
                t.append("│", style="#374151")
            t.append("\n")

        zone = zone_for_hr(int(self._needle["hr"]) or 60, cfg.zones(), cfg.computed_hrmax())
        row([("⏱", fmt_clock(s.elapsed_s), "b white"),
             ("KCAL", f"{s.total_kcal:.0f}", "b #22c55e"),
             ("DIST", f"{s.dist_km:.2f} km", SPD_COLOR)])
        row([("FC MED", f"{s.avg_hr:.0f}" if s.n else "--", "b #facc15"),
             ("FC MAX", f"{s.max_hr or '--'}", "b #ef4444"),
             ("%FCmáx", f"{s.pct_max:.0f}%·{cfg.computed_hrmax()}" if s.n else "--", "#9ca3af")])
        row([("W MED", f"{s.avg_watts:.0f}" if s.avg_watts else "--", "b #facc15"),
             ("W MAX", f"{s.max_watts or '--'}", "b #f97316"),
             ("FTP", f"{cfg.ftp}W", "b #22c55e")])
        row([("ZONA", f"Z{zone.idx} {zone.name}", f"b {zone.color}"),
             ("EN ZONA", fmt_clock(s.zone_seconds[zone.idx]), zone.color),
             ("MUESTRAS", str(s.n), "#9ca3af")])
        if self.tr is not None and not getattr(self.tr, "connected", False):
            t.append(" sin telemetria del rodillo\n", style="italic #f97316")
        if s.n and s.total_kcal:
            t.append(f" ritmo {s.total_kcal / max(1, s.elapsed_s) * 60:.1f} kcal/min"
                     f" · FCmáx est {cfg.computed_hrmax()} · {cfg.age}a/{cfg.weight:.0f}kg {cfg.sex}",
                     style="#6b7280")
        self.query_one("#g_panel", Static).update(t)

    # ---- vista pista: ovalo + tablero de vueltas ----
    def _zone_at(self, idx: int):
        zones = self.config.zones()
        return zones[min(max(idx, 0), 5)]

    def _tick_pista(self) -> None:
        """Marcador de la bici a 10 fps: interpola entre las muestras de 1 Hz."""
        if self.view != "pista":
            return
        try:
            lap_m = max(1, self.laps.lap_m)
            target = self.laps.frac
            spd = self.session.current_speed or (getattr(self.tr, "speed_kmh", None) or 0.0)
            if spd:
                target = min(0.9999, target + (spd / 3.6) * 0.1 / lap_m)
            d = self._marker
            if abs(target - d) > 0.5:        # salto de vuelta: no rebobinar la estela
                d = target
            else:
                d += (target - d) * 0.45
            self._marker = max(0.0, min(0.9999, d))
            self._trail.append(self._marker)
            del self._trail[:-14]
            self._safe_draw(self._draw_pista)
        except Exception as exc:  # noqa: BLE001 - la animacion jamas debe tumbar la app
            self._show_error(f"pista: {type(exc).__name__}: {exc}")

    def _draw_pista(self, hr=None, zone=None) -> None:
        """Ovalo tipo velodromo: la bici sobre el trazado en la vuelta en curso."""
        lap = self.laps
        st = self.query_one("#pista", Static)
        w, h = int(st.size.width or 60), int(st.size.height or 12)
        lap_km = lap.lap_m / 1000.0
        cur = lap.current
        zone = zone or self._zone_at(cur.zone_idx or
                                     zone_for_hr(int(hr or 60), self.config.zones(),
                                                 self.config.computed_hrmax()).idx)
        label = f"V{lap.lap_no + 1} · {lap.pos_m / 1000:.2f}/{lap_km:.2f} km"
        if cur.dist_m:
            sub = (f"{fmt_clock(cur.elapsed_s)} · {cur.avg_hr:.0f} bpm med "
                   f"· {cur.avg_watts:.0f} W · {cur.avg_speed:.1f} km/h"
                   + (" · dist estimada" if lap.estimada else ""))
        else:
            sub = "sin distancia: el rodillo no manda 2ACF/2A5B o no pedaleas"
        st.update(self._track.render(w, h, self._marker, label, sub, zone.color,
                                     trail=self._trail, lap_km=lap_km))

    def _draw_laps(self, hr=None, zone=None) -> None:
        """Tablero de vueltas: flash de la ultima cerrada + vuelta en curso + historial."""
        lap, cfg = self.laps, self.config
        t = Text()
        lap_km = lap.lap_m / 1000.0

        flash = lap.just_lapped and time.time() - self._lap_flash < 12
        if flash:
            zc = self._zone_at(lap.just_lapped.zone_idx).color
            t.append(" ⚑ ", style=f"bold {zc}")
            t.append(f"vuelta {len(lap.laps)} cerrada ", style=f"bold {zc}")
            t.append(f"{fmt_clock(lap.just_lapped.elapsed_s)} · ", style="#9ca3af")
            t.append(f"{lap.just_lapped.avg_hr:.0f} bpm med", style="#facc15")
            t.append(f" · {lap.just_lapped.avg_watts:.0f} W", style="#f97316")
        t.append("\n")

        cur = lap.current
        zone = zone or self._zone_at(cur.zone_idx)
        faltan = max(0.0, lap.lap_m - cur.dist_m) / 1000.0
        t.append(f" EN PISTA  V{lap.lap_no + 1} ", style=f"bold {zone.color}")
        t.append(f"{cur.dist_m / 1000:.2f}/{lap_km:.2f} km ", style=SPD_COLOR)
        t.append(f"faltan {faltan:.2f} ", style="#9ca3af")
        t.append("│", style="#374151")
        t.append(f" {fmt_clock(cur.elapsed_s)} ", style="b white")
        t.append(f" FCmed {cur.avg_hr:.0f} ", style="#facc15")
        t.append(f" Wmed {cur.avg_watts:.0f} ", style="#f97316")
        t.append(f" {cur.avg_speed:.1f} km/h ", style=SPD_COLOR)
        t.append("\n")
        t.append(f" VUELTAS {len(lap.laps)} ", style="b #9ca3af")
        t.append(f"· largo {lap_km:.2f} km · total {lap.dist_m / 1000:.2f} km ", style="#6b7280")
        t.append("· " + ("distancia del rodillo" if not lap.estimada
                         else "distancia estimada (velocidad)"), style="#6b7280")
        t.append("\n")
        for i in range(max(0, len(lap.laps) - 4), len(lap.laps)):
            lp = lap.laps[i]
            zc = self._zone_at(lp.zone_idx).color
            t.append(f"  V{i + 1} ", style=f"bold {zc}")
            t.append(f"{fmt_clock(lp.elapsed_s):>7} ", style="#9ca3af")
            t.append(f"avg {lp.avg_hr:>3.0f} ", style="#facc15")
            t.append(f"max {lp.max_hr:>3} ", style="b #ef4444")
            t.append(f"{lp.avg_watts:>4.0f}W ", style="#f97316")
            t.append(f"{lp.kj:>5.1f}kJ ", style="#9ca3af")
            t.append(f"{lp.dist_m / 1000:>5.2f}km ", style=SPD_COLOR)
            t.append(f"{lp.avg_cadence:>3.0f}rpm ", style=CAD_COLOR)
            t.append(f"{self._zone_at(lp.zone_idx).name[:11]}", style=zc)
            t.append("\n")
        self.query_one("#lapboard", Static).update(t)
