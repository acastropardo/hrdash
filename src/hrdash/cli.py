"""Interfaz de linea de comandos de HRDash."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
import time

from . import __version__
from .ble import (SensorSource, SimSensor, TrainerSource, is_address, probe,
                  scan_heart_rate_sensors, scan_trainers, sim_factory, sim_scan)
from .config import Config, load_config, prompt_interactive, save_config
from .engine import Engine, SCENES_VIEWS
from .history import Session


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hrdash",
        description="Dashboard de FC + telemetria (watts/velocidad/cadencia): ventana 3D "
                    "(por defecto) o consola (--ascii).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--age", type=int, help="edad (default 47 o config)")
    p.add_argument("--sex", choices=["M", "F", "m", "f"], help="sexo (default M)")
    p.add_argument("--weight", type=float, help="peso en kg (default 70 o config)")
    p.add_argument("--ftp", type=int, help="FTP en watts para colorear potencia (default 240)")
    p.add_argument("--hrmax", type=int, help="FCmax fija (default: Tanaka por edad)")
    p.add_argument("--wheel-mm", type=int,
                   help="perimetro de rueda en mm para velocidad via CSC (default 2135)")
    p.add_argument("--lap", type=float, metavar="KM",
                   help="largo de vuelta de la vista pista (tecla p), en km (default 5)")
    p.add_argument("--sensor", metavar="NOMBRE_O_ADDR",
                   help="banda de FC: nombre o direccion (default: autoscaneo)")
    p.add_argument("--trainer", metavar="NOMBRE_O_ADDR",
                   help="rodillo (Tacx): nombre o direccion (default: autoscaneo)")
    p.add_argument("--no-trainer", action="store_true", help="no buscar rodillo")
    # Los modos son excluyentes. Sin ninguno arranca el 3D standalone (motor + ventana, sin
    # consola); `--ascii` es la TUI de siempre. --simulate si se combina (fuera del grupo):
    # es la fuente, no una ruta.
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--scan", nargs="?", const=8, type=int, metavar="SEG",
                       help="listar sensores de FC y rodillos detectados y salir")
    modes.add_argument("--probe", nargs="?", const=12, type=int, metavar="SEG",
                       help="volcar GATT y payloads del dispositivo (--trainer/--sensor) y salir")
    modes.add_argument("--duration", type=int, metavar="SEG",
                       help="modo headless: muestrear N segundos, grabar y salir")
    modes.add_argument("--3d", dest="threed", action="store_true",
                       help="3D standalone (el modo por defecto): motor + ventana GLFW, sin "
                            "consola; falla si el 3D no esta disponible")
    modes.add_argument("--ascii", action="store_true",
                       help="consola (TUI de Textual) en la terminal, sin ventana 3D")
    p.add_argument("--now", action="store_true",
                   help="modo 3D: salta el menu y empieza a rodar ya (scripts, pruebas)")
    p.add_argument("--simulate", action="store_true",
                   help="sensor+rodillo simulados (sin BLE) para probar el dashboard")
    p.add_argument("--3d-view", choices=list(SCENES_VIEWS), action="append",
                   metavar="VISTA", help="vista inicial del modo 3D, repetible para partir la "
                                          "ventana (default: gauge, los tacometros)")
    p.add_argument("--export-dir", help="carpeta de exportacion (default ./exportes)")
    p.add_argument("--no-fit", action="store_true", help="no grabar archivo FIT")
    p.add_argument("--no-csv", action="store_true", help="no grabar CSV espejo")
    p.add_argument("--fresh", action="store_true",
                   help="preguntar edad/sexo/peso aunque esten guardados")
    p.add_argument("--version", action="version", version=f"hrdash {__version__}")
    return p


def apply_flags(cfg: Config, args) -> Config:
    if args.age is not None:
        cfg.age = args.age
    if args.sex is not None:
        cfg.sex = args.sex.upper()
    if args.weight is not None:
        cfg.weight = args.weight
    if args.ftp is not None:
        cfg.ftp = args.ftp
    if args.hrmax is not None:
        cfg.hrmax = args.hrmax
    if args.wheel_mm is not None:
        cfg.wheel_mm = args.wheel_mm
    if args.lap is not None:
        cfg.lap_m = int(max(100, args.lap * 1000))
    if args.export_dir is not None:
        cfg.export_dir = args.export_dir
    if args.no_fit:
        cfg.export_fit = False
    if args.no_csv:
        cfg.export_csv = False
    return cfg


async def _print_devices(title: str, devices) -> None:
    print(f"\n{title}")
    if not devices:
        print("  (nada detectado)")
        return
    for d in devices:
        print(f"  {d.address:<40} {d.name or '(sin nombre)':<28} RSSI {d.rssi}")


async def cmd_scan(seconds: int) -> int:
    from .ble import describe_scan_error, scan_all
    print(f"Escaneando {seconds}s (bandas de FC y rodillos)...")
    try:
        hrs, trs = await scan_all(timeout=seconds)
    except Exception as exc:  # noqa: BLE001 - sin Bluetooth o sin permiso: un motivo, no un traceback
        print(f"No se pudo escanear: {describe_scan_error(exc).lower()} ({type(exc).__name__}).")
        return 2
    await _print_devices("Bandas/pulsometros de FC:", hrs)
    await _print_devices("Rodillos (Cycling Power / FTMS):", trs)
    if hrs or trs:
        print("\nEjemplo:  hrdash --sensor \"R+2.0\" --trainer \"Tacx\"")
        return 0
    print("Consejos: Scosche solo transmite puesto; Tacx en modo BLE (app Tacx: "
          "Ajustes > conexion > Bluetooth).")
    return 1


async def cmd_headless(hr_source, tr_source, cfg: Config, duration: int) -> int:
    eng = Engine(hr_source, tr_source, cfg)
    eng.start()
    print(f"Headless {duration}s · FC={hr_source.name}"
          + (f" · rodillo={tr_source.name}" if tr_source else ""))
    try:
        for i in range(duration):
            await asyncio.sleep(1.0)
            s, closed = eng.poll()
            if s is None:
                print(f"[{i+1:3d}s] esperando FC... {getattr(hr_source, 'error', '')}")
                continue
            print(f"[{i+1:3d}s] FC={s.hr:3d} Z{s.zone_idx} · "
                  f"{s.watts if s.watts is not None else '-':>4} W · "
                  f"{s.speed_kmh if s.speed_kmh is not None else '-':>5} km/h · "
                  f"{s.cadence if s.cadence is not None else '-':>5} rpm · "
                  f"{(s.dist_m or 0)/1000:5.2f} km · "
                  f"media={eng.session.avg_hr:5.1f} · "
                  f"kcal={eng.session.total_kcal:5.2f} · "
                  f"{'OK' if hr_source.connected else 'reconectando FC'}")
            if closed:
                print(f"        ⚑ vuelta {len(eng.laps.laps)} en {closed.elapsed_s}s · "
                      f"media {closed.avg_hr:.0f} bpm · {closed.avg_watts:.0f} W")
    finally:
        await eng.stop()
    res = eng.export()
    print_summary(eng.session, res)
    if eng.session.n:
        print(f"  vueltas de {cfg.lap_m} m: {len(eng.laps.laps)} cerradas · "
              f"en curso {eng.laps.pos_m} m"
              + (" (distancia estimada por velocidad)" if eng.laps.estimada else ""))
    return 0 if eng.session.n else 2


def print_summary(sess: Session, res: dict) -> None:
    print("\n=== Resumen de sesion ===")
    print(f"  duracion: {sess.elapsed_s}s · muestras: {sess.n} · "
          f"FC media: {sess.avg_hr:.1f} · FC max: {sess.max_hr} · FC min: {sess.min_hr}")
    if sess.max_watts:
        print(f"  watts: media {sess.avg_watts:.0f} · max {sess.max_watts} · "
              f"vel media {sess.avg_speed:.1f} km/h · dist {sess.dist_km:.2f} km · "
              f"cad media {sess.avg_cadence:.0f} rpm")
    print(f"  calorias (Keytel): {sess.total_kcal:.1f} kcal")
    if res.get("csv"):
        print(f"  CSV : {res['csv']}")
    if res.get("fit"):
        v = res.get("validate") or {}
        ok = "OK" if v.get("ok") else "REVISAR"
        print(f"  FIT : {res['fit']}  (round-trip {ok}: {v.get('records')}/{v.get('samples')} records)")
    if res.get("fit_error"):
        print(f"  FIT error: {res['fit_error']}")
    if not res.get("csv") and not res.get("fit"):
        print("  (sin exportacion: no hay muestras)")


def cmd_tui(hr_source, tr_source, cfg: Config) -> int:
    from .ui import HRDash
    app = HRDash(hr_source, tr_source, cfg)
    app.run()
    print_summary(app.session, app.export_result or {})
    changed = False
    if getattr(hr_source, "name", "") and not isinstance(hr_source, SimSensor) \
            and getattr(hr_source, "connected", False):
        cfg.last_device = hr_source.name
        changed = True
    if tr_source is not None and not isinstance(tr_source, SimSensor) \
            and getattr(tr_source, "connected", False):
        cfg.last_trainer = tr_source.name
        changed = True
    if changed:
        save_config(cfg)
    return 0


async def cmd_3d(hr_source, tr_source, cfg: Config, views, seconds: int | None = None,
                 host=None, menu: bool = False, scanner=None, factory=None) -> int:
    """Modo 3D (por defecto o `--3d`): motor + ventana, sin TUI. No hay terminal al que
    reenviar teclas.

    Con `menu=True` la ventana abre en el menu previo: las fuentes conectan y se ven en vivo, pero
    no se graba nada hasta que la ventana manda `{"ui": {"act": "play"}}` (clic o Enter).

    El motor es el dueño del estado (Session/LapTracker/export); la ventana es un proceso
    hijo que habla JSON por tuberia (proto.py). La ventana gestiona aqui su teclado y su
    camara: `h/w/g/p` y `3`/`r` cambian la vista, `+`/`-` y la rueda hacen zoom. Lo que
    afecta a la sesion se reenvia y lo procesa el motor: `q` sale y graba, `x` resetea,
    `s` reconecta. Con TUI la dueña de los bindings sigue siendo `ui.py`; este camino no
    toca la TUI, y por eso no colisionan.

    Si la ventana cae, el muestreo sigue (la app no muere por un fallo de render) y al
    salir se exporta igual que en la TUI: CSV antes que FIT.
    """
    from .glview import GLHost
    host = host or GLHost()
    eng = Engine(hr_source, tr_source, cfg, menu=menu, scanner=scanner, factory=factory)
    eng.views = [v for v in (views or ["gauge"]) if v in SCENES_VIEWS]
    if not host.start():
        print(f"3D: {host.error or '3D no disponible: pip install -e \".[gl]\"'}")
        return 2
    eng.start()
    if menu:
        eng.request_scan(fresh=False)        # lista inicial (comparte el escaneo de las fuentes)
    print(f"3D standalone · {'menu -> ' if menu else ''}vistas: {'+'.join(eng.views)} · FC={getattr(hr_source, 'name', '')}"
          + (f" · rodillo={getattr(tr_source, 'name', '')}" if tr_source else "")
          + " · `q` en la ventana o Ctrl+C para salir y grabar · `v` camara · `i` tacometros")
    warned = False
    quit_now = False
    opened = False        # primer mensaje `win` del hijo: la ventana existe
    never_opened = False  # err antes de win: sin display, esta ruta no tiene sentido
    last_push = 0.0
    try:
        while seconds is None or seconds > 0:
            # En el menu se sondea a 10 Hz (un clic no puede esperar 1 s) y el snapshot sale a
            # ~2 Hz mas uno inmediato tras cada evento; rodando, todo a 1 Hz como siempre.
            tick = 0.1 if eng.phase == "menu" else 1.0
            await asyncio.sleep(tick)
            s, closed = eng.poll()
            if seconds is not None:
                seconds -= tick
            now = time.monotonic()
            if eng.phase != "menu" or now - last_push >= 0.5:
                last_push = now
                try:
                    host.push(eng.snapshot())
                except Exception as exc:  # noqa: BLE001 - la ventana no interrumpe el muestreo
                    if not warned:
                        print(f"3D push: {type(exc).__name__}: {exc}"[:80])
                        warned = True
            ui_changed = False
            for m in host.drain():
                if m.get("win"):
                    opened = True
                ui = m.get("ui")
                if isinstance(ui, dict) and await eng.handle_ui(ui):
                    ui_changed = True
                if m.get("key") and eng.handle_key(m["key"]):
                    quit_now = True
                    break
                got = m.get("views")
                if isinstance(got, list):
                    eng.views = [v for v in got if v in SCENES_VIEWS] or eng.views
                if m.get("err"):
                    if not opened:
                        print(f"3D: {m['err']}"[:80])
                        never_opened = True
                        break
                    if not warned:
                        print(f"3D: {m['err']}"[:80])
                        warned = True
            if ui_changed:                   # la ventana ve el cambio de fase sin esperar al tick
                last_push = time.monotonic()
                try:
                    host.push(eng.snapshot())
                except Exception:  # noqa: BLE001
                    pass
            if quit_now or never_opened:
                break
            if not host.alive():
                if not opened:
                    never_opened = True
                    break    # murio sin llegar a abrir: nada que mostrar, salir
                if not warned:
                    print("3D: ventana cerrada; el motor sigue muestreando hasta Ctrl+C")
                    warned = True
                if seconds is None:
                    break        # sin ventana y sin limite: no hay nada que mostrar
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Con Python >= 3.11, `asyncio.run` no lanza KeyboardInterrupt dentro de la corrutina:
        # cancela la tarea y lo relanza al final, asi que sin capturar CancelledError el Ctrl+C
        # se saltaba la exportacion. Se quita la cancelacion para que stop/export corran.
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            task.uncancel()
        print("\n3D: Ctrl+C" + (", grabando..." if eng.phase != "menu" else ""))
    finally:
        # Guardar ANTES de parar: al parar, cada fuente pasa a `connected = False` y el ultimo
        # sensor/rodillo nunca llegaba a guardarse (antes se hacia despues de `eng.stop()`).
        try:
            persist_choices(eng, cfg)
        except Exception:  # noqa: BLE001 - no poder guardar la config no debe impedir exportar
            pass
        await eng.stop()
        host.stop()
    if eng.phase == "menu":                  # salio del menu sin pulsar Play: no hay sesion
        print("Saliste del menu sin iniciar: no se grabo nada.")
        return 2 if never_opened else 0
    res = eng.export()
    print_summary(eng.session, res)
    if eng.workout is not None:              # el plan elegido en el menu (la fase 4 lo sigue en vivo)
        print(f"  plan: {eng.spec.summary().capitalize()}")
    return 2 if never_opened or not eng.session.n else 0


def gl_unavailable() -> str | None:
    """Motivo por el que el 3D no puede arrancar aqui, o None si puede.

    Solo mira lo barato (paquetes del extra `gl` instalados y, en Linux, que haya display):
    sirve para que el modo por defecto caiga a la consola en vez de fallar. Que la ventana
    abra de verdad lo decide el proceso hijo.
    """
    missing = [m for m in ("moderngl", "glfw", "numpy") if importlib.util.find_spec(m) is None]
    if missing:
        return "faltan " + ", ".join(missing) + ' (pip install -e ".[gl]")'
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY")
                                                 or os.environ.get("WAYLAND_DISPLAY")):
        return "no hay display (DISPLAY/WAYLAND_DISPLAY)"
    return None


def source_ident(src) -> str:
    """Lo que se guarda de una fuente para reconectar: su direccion si se eligio por direccion
    (distingue dos dispositivos con el mismo nombre) y si no, el nombre."""
    target = getattr(src, "target", None) or ""
    return target if is_address(target) else src.name


def persist_last(hr_source, tr_source, cfg: Config) -> bool:
    """Guarda el ultimo sensor/rodillo usado (lo mismo que hace la TUI al salir). True si cambio."""
    changed = False
    if getattr(hr_source, "name", "") and not isinstance(hr_source, SimSensor) \
            and getattr(hr_source, "connected", False):
        cfg.last_device = source_ident(hr_source)
        changed = True
    if tr_source is not None and not isinstance(tr_source, SimSensor) \
            and getattr(tr_source, "connected", False):
        cfg.last_trainer = source_ident(tr_source)
        changed = True
    return changed


def persist_choices(eng, cfg: Config) -> None:
    """Guarda lo elegido en el menu: ultimos dispositivos y si se usa rodillo. Una sola escritura."""
    changed = persist_last(eng.hr, eng.tr, cfg)
    if eng.trainer_pref is not None and cfg.use_trainer != eng.trainer_pref:
        cfg.use_trainer = eng.trainer_pref
        changed = True
    if eng.spec != cfg.session:
        cfg.session = eng.spec
        changed = True
    if changed:
        save_config(cfg)


async def _probe_wrapper(target, seconds: int, trainer: bool) -> int:
    print(f"Probe {'rodillo' if trainer else 'banda'}: buscando...", flush=True)
    await probe(target=target, seconds=seconds, trainer=trainer)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    if args.fresh:
        cfg = Config()
    cfg = apply_flags(cfg, args)

    non_interactive = bool(args.duration) or not sys.stdout.isatty()
    asked = all(x is not None for x in (args.age, args.sex, args.weight))
    if not asked and args.scan is None and args.probe is None:
        cfg = prompt_interactive(cfg, non_interactive=non_interactive)
    save_config(cfg)

    if args.scan is not None:
        return asyncio.run(cmd_scan(args.scan))

    if args.probe is not None:
        target = args.trainer or args.sensor or cfg.last_trainer or cfg.last_device
        return asyncio.run(_probe_wrapper(target, args.probe,
                                          trainer=bool(args.trainer) or not args.sensor))

    if args.simulate:
        hr_source = SimSensor()
        tr_source = None if args.no_trainer else hr_source   # el simulador da todo
    else:
        hr_source = SensorSource(args.sensor or cfg.last_device)
        tr_source = None
        if not args.no_trainer and (args.trainer or cfg.use_trainer):
            tr_source = TrainerSource(args.trainer or cfg.last_trainer,
                                      wheel_mm=cfg.wheel_mm)

    if args.duration:
        return asyncio.run(cmd_headless(hr_source, tr_source, cfg, args.duration))

    # Sin modo explicito arranca el 3D; si aqui no puede (sin paquetes GL o sin display),
    # cae a la consola en vez de fallar. Con --3d explicito no hay caida: falla y avisa.
    use_3d = args.threed
    if not args.ascii and not args.threed:
        why = gl_unavailable()
        if why is None:
            use_3d = True
        else:
            print(f"3D no disponible ({why}): arranco la consola (--ascii).")
    if use_3d:
        # Standalone: no hace falta TTY para el motor; la ventana la pone el proceso hijo.
        # Si el entorno no da para GL, cmd_3d avisa y sale con 2 (la TUI no se toca).
        # El dest de "--3d-view" es "3d_view", no un identificador valido: getattr.
        return asyncio.run(cmd_3d(hr_source, tr_source, cfg,
                                  getattr(args, "3d_view") or ["gauge"], menu=not args.now,
                                  scanner=sim_scan if args.simulate else None,
                                  factory=sim_factory if args.simulate else None))

    if not sys.stdout.isatty():
        print("La consola (--ascii) necesita un terminal interactivo. Usa --duration para "
              "modo headless.")
        return 2
    return cmd_tui(hr_source, tr_source, cfg)


if __name__ == "__main__":
    sys.exit(main())
