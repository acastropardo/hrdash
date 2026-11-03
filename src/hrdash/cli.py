"""Interfaz de linea de comandos de HRDash."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from . import __version__
from .ble import SensorSource, SimSensor, scan_heart_rate_sensors
from .config import Config, load_config, prompt_interactive, save_config
from .export import export_session
from .history import Session


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hrdash",
        description="Dashboard de frecuencia cardiaca en terminal (sensor BLE estandar).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--age", type=int, help="edad (default 47 o config)")
    p.add_argument("--sex", choices=["M", "F", "m", "f"], help="sexo (default M)")
    p.add_argument("--weight", type=float, help="peso en kg (default 70 o config)")
    p.add_argument("--hrmax", type=int, help="FCmax fija (default: Tanaka por edad)")
    p.add_argument("--sensor", metavar="NOMBRE_O_ADDR",
                   help="nombre o direccion del sensor (default: autoscaneo)")
    p.add_argument("--scan", nargs="?", const=8, type=int, metavar="SEG",
                   help="listar sensores de FC detectados y salir")
    p.add_argument("--simulate", action="store_true",
                   help="sensor simulado (sin BLE) para probar el dashboard")
    p.add_argument("--duration", type=int, metavar="SEG",
                   help="modo headless: muestrear N segundos, grabar y salir")
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
    if args.hrmax is not None:
        cfg.hrmax = args.hrmax
    if args.export_dir is not None:
        cfg.export_dir = args.export_dir
    if args.no_fit:
        cfg.export_fit = False
    if args.no_csv:
        cfg.export_csv = False
    return cfg


async def cmd_scan(seconds: int) -> int:
    print(f"Escaneando {seconds}s (¿el Rhythm esta puesto y encendido?)...")
    devices = await scan_heart_rate_sensors(timeout=seconds)
    if not devices:
        print("No se detectaron sensores de FC. Consejos:")
        print(" - El Scosche Rhythm+ solo transmite cuando esta puesto en el brazo/pecho.")
        print(" - macOS: Ajustes > Privacidad > Bluetooth: habilita tu terminal.")
        print(" - Linux: systemctl status bluetooth y tu usuario en el grupo bluetooth.")
        return 1
    print(f"{'DIRECCION/UUID':<40} NOMBRE")
    for d in devices:
        print(f"{d.address:<40} {d.name or '(sin nombre)'}")
    print("\nConectate con:  hrdash --sensor \"<nombre o direccion>\"")
    return 0


async def cmd_headless(sensor, cfg: Config, duration: int) -> int:
    sess = Session(cfg)
    stop = asyncio.Event()
    task = asyncio.create_task(sensor.run(stop))
    print(f"Headless {duration}s · sensor={sensor.name} · FCmáx est {cfg.computed_hrmax()}")
    try:
        for i in range(duration):
            await asyncio.sleep(1.0)
            hr = getattr(sensor, "latest_hr", None)
            if hr is None:
                print(f"[{i+1:3d}s] esperando lectura... {getattr(sensor, 'error', '')}")
                continue
            s = sess.add(hr)
            zkey = f"Z{s.zone_idx}"
            print(f"[{i+1:3d}s] FC={hr:3d} {zkey} · media={sess.avg_hr:5.1f} "
                  f"max={sess.max_hr} · kcal={sess.total_kcal:5.2f} "
                  f"· {'OK' if sensor.connected else 'reconectando'}")
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    res = export_session(sess, cfg, sensor.name)
    print_summary(sess, res)
    return 0 if sess.n else 2


def print_summary(sess: Session, res: dict) -> None:
    print("\n=== Resumen de sesion ===")
    print(f"  duracion: {sess.elapsed_s}s · muestras: {sess.n} · "
          f"media: {sess.avg_hr:.1f} · max: {sess.max_hr} · min: {sess.min_hr}")
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


def cmd_tui(sensor, cfg: Config) -> int:
    from .ui import HRDash
    app = HRDash(sensor, cfg)
    app.run()
    res = app.export_result or {}
    print_summary(app.session, res)
    if getattr(sensor, "name", "") and not isinstance(sensor, SimSensor):
        cfg.last_device = sensor.name
        save_config(cfg)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    if args.fresh:
        cfg = Config()
    cfg = apply_flags(cfg, args)

    non_interactive = bool(args.duration) or not sys.stdout.isatty()
    asked = all(x is not None for x in (args.age, args.sex, args.weight))
    if not asked and args.scan is None:
        cfg = prompt_interactive(cfg, non_interactive=non_interactive)
    save_config(cfg)

    if args.scan is not None:
        return asyncio.run(cmd_scan(args.scan))

    if args.simulate:
        sensor = SimSensor()
    else:
        sensor = SensorSource(args.sensor or cfg.last_device)

    if args.duration:
        return asyncio.run(cmd_headless(sensor, cfg, args.duration))

    if not sys.stdout.isatty():
        print("La TUI necesita un terminal interactivo. Usa --duration para modo headless.")
        return 2
    return cmd_tui(sensor, cfg)


if __name__ == "__main__":
    sys.exit(main())
