"""Dashboard a pantalla completa (Textual): numero gigante, barras por segundo, zonas."""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static
from rich.text import Text

from .bigfont import render_big
from .config import Config, save_config
from .history import Session
from .export import export_session
from .zones import zone_for_hr


def fmt_clock(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class ConfigModal(ModalScreen):
    """Cambiar edad / sexo / peso sin reiniciar la sesion."""

    CSS = """
    #cfgbox { width: 46; height: auto; padding: 1 2; background: $panel; border: round $brand; }
    Label { color: $text-muted; }
    Input { margin: 0 0 1 0; }
    #err { color: #ef4444; height: 1; }
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        with Vertical(id="cfgbox"):
            yield Label("Edad")
            yield Input(placeholder="47", id="in_age")
            yield Label("Sexo (M/F)")
            yield Input(placeholder="M", id="in_sex")
            yield Label("Peso (kg)")
            yield Input(placeholder="70", id="in_weight")
            yield Static("", id="err")
            with Horizontal():
                yield Button("Guardar", id="ok", variant="success")
                yield Button("Cancelar", id="no", variant="default")

    def on_mount(self) -> None:
        self.query_one("#in_age", Input).value = str(self.cfg.age)
        self.query_one("#in_sex", Input).value = self.cfg.sex
        self.query_one("#in_weight", Input).value = str(self.cfg.weight)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "no":
            self.dismiss(None)
            return
        err = self.query_one("#err", Static)
        try:
            age = int(self.query_one("#in_age", Input).value)
            sex = self.query_one("#in_sex", Input).value.strip().upper()[:1]
            weight = float(self.query_one("#in_weight", Input).value)
            assert 5 <= age <= 120 and sex in ("M", "F") and 20 <= weight <= 400
        except Exception:  # noqa: BLE001
            err.update("Valores invalidos (edad 5-120, M/F, peso 20-400)")
            return
        self.cfg.age, self.cfg.sex, self.cfg.weight = age, sex, weight
        self.dismiss(self.cfg)


class HRDash(App):
    TITLE = "HRDash"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("q", "quit_session", "Salir y grabar"),
        ("r", "reset_session", "Reiniciar"),
        ("s", "reconnect", "Reconectar sensor"),
        ("c", "open_config", "Config"),
    ]

    CSS = """
    Screen { padding: 0 1; }
    #status { height: 1; color: #d1d5db; }
    #midrow { height: 5; }
    #bignum { width: 1fr; height: 1fr; }
    #sideinfo { width: 38; height: 1fr; padding-left: 2; }
    #chart { height: 1fr; }
    #chart_title { height: 1; color: #6b7280; }
    #stats { height: 2; }
    #zones { height: 6; }
    #hints { height: 1; color: #6b7280; }
    """

    def __init__(self, sensor, config: Config):
        super().__init__()
        self.sensor = sensor
        self.config = config
        self.session = Session(config)
        self._stop = asyncio.Event()
        self.export_result: dict | None = None

    # ---- layout ----
    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="midrow"):
            yield Static(id="bignum")
            yield Static(id="sideinfo")
        yield Static(id="chart_title")
        yield Static(id="chart")
        yield Static(id="stats")
        yield Static(id="zones")
        yield Static(id="hints")

    def on_mount(self) -> None:
        self.run_worker(self._run_sensor(), group="sensor", exit_on_error=False)
        self.set_interval(1.0, self.tick)
        self.query_one("#hints", Static).update(
            " q: salir y grabar FIT+CSV · r: reiniciar sesion · s: reconectar sensor · c: edad/sexo/peso")
        self.refresh_all()

    async def _run_sensor(self) -> None:
        try:
            await self.sensor.run(self._stop)
        except Exception as exc:  # noqa: BLE001
            self.sensor.error = f"sensor caido: {exc}"

    # ---- acciones ----
    def action_reset_session(self) -> None:
        self.session.reset()

    def action_reconnect(self) -> None:
        if hasattr(self.sensor, "request_reconnect"):
            self.sensor.request_reconnect()

    def action_open_config(self) -> None:
        self.push_screen(ConfigModal(self.config), self._config_updated)

    def _config_updated(self, cfg: Config | None) -> None:
        if cfg is not None:
            save_config(cfg)
            self.refresh_all()

    def action_quit_session(self) -> None:
        try:
            self.export_result = export_session(self.session, self.config, self.sensor.name)
        except Exception as exc:  # noqa: BLE001
            self.export_result = {"fit_error": str(exc)}
        self._stop.set()
        self.exit()

    # ---- ciclo de refresco (1 Hz) ----
    def tick(self) -> None:
        hr = getattr(self.sensor, "latest_hr", None)
        if hr is not None:
            self.session.add(hr)
        self.refresh_all()

    def refresh_all(self) -> None:
        s = self.session
        cfg = self.config
        hr = s.current_hr if s.n else (getattr(self.sensor, "latest_hr", None) or 0)
        zone = zone_for_hr(hr or 60, cfg.zones(), cfg.computed_hrmax())
        self._draw_status(zone)
        self._draw_bignum(hr, zone)
        self._draw_sideinfo(hr, zone)
        self._draw_chart()
        self._draw_stats()
        self._draw_zones()

    def _draw_status(self, zone) -> None:
        sensor = self.sensor
        ok = getattr(sensor, "connected", False)
        dot, dot_color = ("●", zone.color) if ok else ("○", "#ef4444")
        name = getattr(sensor, "name", "sensor")
        bat = getattr(sensor, "battery", None)
        contact = getattr(sensor, "contact", None)
        err = getattr(sensor, "error", "")
        parts = [
            Text(f"{dot} ", style=f"bold {dot_color}"),
            Text(f"{name}", style="bold white"),
            Text("  " + ("conectado" if ok else f"DESCONECTADO{' - ' + err if err else ''}"),
                 style="#22c55e" if ok else "bold #ef4444"),
        ]
        if bat is not None:
            parts.append(Text(f" · pila {bat}%", style="#9ca3af"))
        if contact is not None:
            parts.append(Text(f" · contacto {'si' if contact else 'NO'}",
                              style="#9ca3af" if contact else "#f97316"))
        parts.append(Text(f" · ⏱ {fmt_clock(self.session.elapsed_s)}", style="#9ca3af"))
        parts.append(Text(f" · Z{zone.idx} {zone.name}", style=f"bold {zone.color}"))
        self.query_one("#status", Static).update(Text.assemble(*parts))

    def _draw_bignum(self, hr: int, zone) -> None:
        label = str(hr) if hr else "--"
        rows = render_big(label)
        t = Text(justify="left")
        for i, r in enumerate(rows):
            t.append(r, style=f"bold {zone.color}")
            if i < len(rows) - 1:
                t.append("\n")
        self.query_one("#bignum", Static).update(t)

    def _draw_sideinfo(self, hr: int, zone) -> None:
        cfg = self.config
        hrmax = cfg.computed_hrmax()
        heart = "♥" if (hr and self.session.n % 2 == 0) else "♡"
        pct = self.session.pct_max if self.session.n else 0.0
        kpm_now = 0.0
        if hr:
            from .calories import keytel_kcal_per_min
            kpm_now = keytel_kcal_per_min(hr, cfg.age, cfg.weight, cfg.sex)
        low = max(0, int(round(zone.low_pct / 100 * hrmax)))
        high = "∞" if zone.high_pct > 200 else str(int(round(zone.high_pct / 100 * hrmax)))
        t = Text()
        t.append(f"{heart} BPM ", style=f"bold {zone.color}")
        t.append(f"zona {zone.key} · {low}-{high if high!='∞' else 'max'}\n", style="#d1d5db")
        t.append(f"%FCmáx  {pct:5.1f} %  (máx est {hrmax})\n", style="#9ca3af")
        t.append(f"kcal/min ahora {kpm_now:4.1f}\n", style="#9ca3af")
        t.append(f"muestras {self.session.n}", style="#6b7280")
        self.query_one("#sideinfo", Static).update(t)

    def _draw_chart(self) -> None:
        cfg = self.config
        chart = self.query_one("#chart", Static)
        width = max(20, chart.size.width or 80)
        rows = max(3, chart.size.height or 10)
        samples = self.session.window(width)
        self.query_one("#chart_title", Static).update(
            f"Barras de FC · 1 muestra/seg · ventana {width}s · suelo 40 · techo "
            f"{max(cfg.computed_hrmax(), (max((s.hr for s in samples), default=0)) + 5)} bpm")

        if not samples:
            chart.update(Text("esperando datos del sensor...", style="#6b7280"))
            return

        top = max(cfg.computed_hrmax(), max(s.hr for s in samples) + 5)
        floor = 40
        span = max(1.0, top - floor)

        def bar_h(hr: int) -> int:
            h = int(round((hr - floor) / span * rows))
            return max(1, min(rows, h)) if hr >= floor else 0

        zones = cfg.zones()
        cols = [bar_h(s.hr) for s in samples]
        zcolors = [zones[min(s.zone_idx, 5)].color for s in samples]

        t = Text()
        for r in range(rows, 0, -1):
            for i, h in enumerate(cols):
                ch = "█" if h >= r else ("▏" if r == 1 else " ")
                if ch != " ":
                    t.append(ch * 1, style=zcolors[i])
                else:
                    t.append(" ")
            if r > 1:
                t.append("\n")
        chart.update(t)

    def _draw_stats(self) -> None:
        s = self.session
        t = Text()
        def cell(label: str, value: str, style: str = "bold white") -> None:
            t.append(f" {label} ", style="#6b7280")
            t.append(f"{value} ", style=style)
            t.append("│", style="#374151")
        avg = f"{s.avg_hr:.0f}" if s.n else "--"
        cell("FC MAX", f"{s.max_hr or '--'}", "bold #ef4444")
        cell("FC MEDIA", avg, "bold #facc15")
        cell("FC MIN", f"{s.min_hr or '--'}", "bold #2f7bff")
        cell("TIEMPO", fmt_clock(s.elapsed_s))
        cell("KCAL TOTAL", f"{s.total_kcal:.1f}", "bold #22c55e")
        t.append("\n")
        kpm = s.total_kcal / max(1, s.elapsed_s) * 60 if s.n else 0.0
        cell("KCAL/MIN", f"{kpm:.1f}")
        cell("MUESTRAS", str(s.n))
        cell("FCmáx est", str(self.config.computed_hrmax()))
        cell("EDAD/PESO", f"{self.config.age}a/{self.config.weight:.0f}kg {self.config.sex}")
        self.query_one("#stats", Static).update(t)

    def _draw_zones(self) -> None:
        s = self.session
        zones = self.config.zones()
        barw = 28
        t = Text()
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
