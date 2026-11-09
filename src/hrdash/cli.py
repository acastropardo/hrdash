"""Interfaz de linea de comandos de HRDash."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from . import __version__
from .ble import (SensorSource, SimSensor, TrainerSource, probe,
                  scan_heart_rate_sensors, scan_trainers)
from .config import Config, load_config, prompt_interactive, save_config
from .export import export_session
from .history import Session
from .laps import LapTracker


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hrdash",
        description="Dashboard de FC + telemetria (watts/velocidad/cadencia) en terminal.",
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
    p.add_argument("--scan", nargs="?", const=8, type=int, metavar="SEG",
                   help="listar sensores de FC y rodillos detectados y salir")
    p.add_argument("--probe", nargs="?", const=12, type=int, metavar="SEG",
                   help="volcar GATT y payloads del dispositivo (--trainer/--sensor) y salir")
    p.add_argument("--simulate", action="store_true",
                   help="sensor+rodillo simulados (sin BLE) para probar el dashboard")
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
        print(f"  {d.address:<40} {d.name or '(sin nombre)'}")


async def cmd_scan(seconds: int) -> int:
    from .ble import scan_all
    print(f"Escaneando {seconds}s (bandas de FC y rodillos)...")
    hrs, trs = await scan_all(timeout=seconds)
    await _print_devices("Bandas/pulsometros de FC:", hrs)
    await _print_devices("Rodillos (Cycling Power / FTMS):", trs)
    if hrs or trs:
        print("\nEjemplo:  hrdash --sensor \"R+2.0\" --trainer \"Tacx\"")
        return 0
    print("Consejos: Scosche solo transmite puesto; Tacx en modo BLE (app Tacx: "
          "Ajustes > conexion > Bluetooth).")
    return 1


async def cmd_headless(hr_source, tr_source, cfg: Config, duration: int) -> int:
    sess = Session(cfg)
    laps = LapTracker(cfg.lap_m, cfg)
    stop = asyncio.Event()
    tasks = [asyncio.create_task(hr_source.run(stop))]
    if tr_source is not None:
        tasks.append(asyncio.create_task(tr_source.run(stop)))
    print(f"Headless {duration}s · FC={hr_source.name}"
          + (f" · rodillo={tr_source.name}" if tr_source else ""))
    try:
        for i in range(duration):
            await asyncio.sleep(1.0)
            hr = getattr(hr_source, "latest_hr", None)
            if hr is None:
                print(f"[{i+1:3d}s] esperando FC... {getattr(hr_source, 'error', '')}")
                continue
            w = getattr(tr_source, "watts", None) if tr_source else None
            cad = getattr(tr_source, "cadence", None) if tr_source else None
            spd = getattr(tr_source, "speed_kmh", None) if tr_source else None
            dist = getattr(tr_source, "dist_m", None) if tr_source else None
            s = sess.add(hr, watts=w, cadence=cad, speed_kmh=spd, dist_m=dist)
            closed = laps.add(s)
            print(f"[{i+1:3d}s] FC={hr:3d} Z{s.zone_idx} · {w or '-':>4} W · "
                  f"{spd if spd is not None else '-':>5} km/h · "
                  f"{cad if cad is not None else '-':>5} rpm · "
                  f"{(dist or 0)/1000:5.2f} km · media={sess.avg_hr:5.1f} · "
                  f"kcal={sess.total_kcal:5.2f} · "
                  f"{'OK' if hr_source.connected else 'reconectando FC'}")
            if closed:
                print(f"        ⚑ vuelta {len(laps.laps)} en {closed.elapsed_s}s · "
                      f"media {closed.avg_hr:.0f} bpm · {closed.avg_watts:.0f} W")
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    res = export_session(sess, cfg, hr_source.name)
    print_summary(sess, res)
    if sess.n:
        print(f"  vueltas de {cfg.lap_m} m: {len(laps.laps)} cerradas · "
              f"en curso {laps.pos_m} m"
              + (" (distancia estimada por velocidad)" if laps.estimada else ""))
    return 0 if sess.n else 2


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
        if not args.no_trainer:
            tr_source = TrainerSource(args.trainer or cfg.last_trainer,
                                      wheel_mm=cfg.wheel_mm)

    if args.duration:
        return asyncio.run(cmd_headless(hr_source, tr_source, cfg, args.duration))

    if not sys.stdout.isatty():
        print("La TUI necesita un terminal interactivo. Usa --duration para modo headless.")
        return 2
    return cmd_tui(hr_source, tr_source, cfg)


async def _probe_wrapper(target, seconds: int, trainer: bool) -> int:
    print(f"Probe {'rodillo' if trainer else 'banda'}: buscando...", flush=True)
    await probe(target=target, seconds=seconds, trainer=trainer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
