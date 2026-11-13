"""Historial de carreras (`races.json` junto a `config.toml`): tiempo, parciales y la serie de potencia de cada
persecucion, para el fantasma "mi mejor marca" y para saber si una carrera es record personal.

Escritura atomica (archivo temporal + rename) y lectura tolerante: un archivo roto o con campos raros no impide
arrancar ni correr (se ignora y se vuelve a empezar)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

MAX_RECORDS = 200            # las mas recientes; el archivo no crece sin limite


def _path(path=None) -> Path:
    if path is not None:
        return Path(path)
    from .config import CONFIG_DIR     # import tardio: respeta XDG_CONFIG_HOME cambiado en las pruebas
    return CONFIG_DIR / "races.json"


def _valid(r) -> bool:
    try:
        return (isinstance(r, dict) and r.get("event") == "persecucion" and isinstance(r.get("distance_m"), int)
                and float(r["time_s"]) > 0 and isinstance(r.get("power"), list)
                and all(isinstance(x, (int, float)) and x == x for x in r["power"]))
    except (KeyError, TypeError, ValueError):
        return False


def load(path=None) -> list:
    try:
        data = json.loads(_path(path).read_text())
    except Exception:  # noqa: BLE001 - ausente o roto: sin historial
        return []
    return [r for r in data if _valid(r)] if isinstance(data, list) else []


def best(distance_m: int, path=None) -> dict | None:
    """La mejor marca (menor tiempo) en esa distancia, o None."""
    rs = [r for r in load(path) if r["distance_m"] == distance_m]
    return min(rs, key=lambda r: r["time_s"]) if rs else None


def add(record: dict, path=None) -> bool:
    """Guarda una carrera. True si es record personal en su distancia (o la primera)."""
    if not _valid(record):
        return False
    rs = load(path)
    prev = [r["time_s"] for r in rs if r["distance_m"] == record["distance_m"]]
    pb = not prev or record["time_s"] < min(prev)
    rs.append(record)
    p = _path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rs[-MAX_RECORDS:]))
        os.replace(tmp, p)
    except OSError:
        return pb
    return pb


def record_from_race(r, now: float | None = None) -> dict:
    """El registro de una carrera terminada (`PursuitRace` en `finished`)."""
    return {"event": "persecucion", "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(now or time.time())),
            "distance_m": int(r.spec.distance_m), "time_s": round(float(r.final_s), 2),
            "splits": [round(x, 2) for x in r.splits], "power": [round(x, 1) for x in r.power_series]}
