"""Historial de carreras (`races.json` junto a `config.toml`): tiempo, parciales y la serie de potencia de cada
persecucion, para el fantasma "mi mejor marca" y para saber si una carrera es record personal. Tambien guarda el
resultado de cada scratch y prueba de puntos (puesto, tiempo, puntos, potencia media) y resume todo para el menu
(`summary`): records por prueba, tendencia y ultimas carreras.

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


PELOTON_EVENTS = ("scratch", "points", "elimination")


def _valid(r) -> bool:
    try:
        if not isinstance(r, dict) or not isinstance(r.get("date"), str):
            return False
        if r.get("event") in PELOTON_EVENTS:
            return (isinstance(r.get("laps"), int) and isinstance(r.get("pos"), int) and 1 <= r["pos"] <= 99
                    and float(r["time_s"]) > 0 and isinstance(r.get("level", "even"), str)
                    and isinstance(r.get("pts", 0), int) and isinstance(r.get("n", 4), int))
        return (r.get("event") == "persecucion" and isinstance(r.get("distance_m"), int)
                and float(r["time_s"]) > 0 and isinstance(r.get("power"), list)
                and all(isinstance(x, (int, float)) and x == x for x in r["power"]))
    except (KeyError, TypeError, ValueError):
        return False


def _key(r) -> tuple:
    """La categoria de un registro (los records se comparan solo dentro de ella)."""
    if r["event"] == "persecucion":
        return ("persecucion", r["distance_m"])
    return (r["event"], r["laps"], _norm(r.get("level", "even")))


def _score(r) -> float:
    """Menor es mejor: el tiempo en la persecucion y el scratch; en puntos, mas puntos y a igualdad menos tiempo."""
    lost = 0.0 if r["event"] == "persecucion" or r.get("pos") == 1 else 1e5      # una derrota no es marca
    if r["event"] == "points":
        return lost - 1000.0 * r.get("pts", 0) + r["time_s"] / 1000.0
    return lost + r["time_s"]


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
    prev = [_score(r) for r in rs if _key(r) == _key(record)]
    pb = not prev or _score(record) < min(prev)
    if record["event"] in PELOTON_EVENTS:
        pb = pb and record["pos"] == 1          # en el pelotón solo cuenta como record una victoria
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


def record_from_peloton(r, now: float | None = None) -> dict | None:
    """El registro de un scratch / prueba de puntos terminado (`ScratchRace` en `finished`), o None si el jugador no
    tiene resultado."""
    me = next((x for x in getattr(r, "results", []) if x.get("player")), None)
    if me is None or me.get("time_s") is None:
        return None
    ps = [x for x in r.power_series if x == x]
    rec = {"event": r.spec.event, "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(now or time.time())),
           "laps": int(r.spec.eff_laps()), "level": r.spec.level, "pos": int(me["pos"]), "n": len(r.results),
           "time_s": round(float(me["time_s"]), 2), "avg_w": round(sum(ps) / len(ps), 1) if ps else 0.0,
           "max_w": round(max(ps), 1) if ps else 0.0}
    if r.spec.event == "points":
        rec["pts"] = int(me.get("pts", 0))
    return rec


_NAMES = {"persecucion": "PERSECUCION", "scratch": "SCRATCH", "points": "PUNTOS", "elimination": "ELIMINACION"}
def _norm(level) -> str:
    """Los registros viejos guardan easy/even/hard: se leen con el nombre de hoy."""
    from . import peloton
    return peloton.norm_level(level) or str(level)


def _label(k: tuple) -> str:
    if k[0] == "persecucion":
        return f"PERSECUCION {k[1] / 1000:g} KM"
    from . import peloton
    return f"{_NAMES[k[0]]} {k[1]} V {peloton.LEVEL_NAMES.get(k[2], str(k[2]).upper())}"


def _value(r) -> str:
    m, sec = divmod(r["time_s"], 60.0)
    clock = f"{int(m)}:{sec:04.1f}"
    if r["event"] == "elimination" and r.get("pos") != 1:
        return "ELIMINADO"
    return f"{r.get('pts', 0)} PTS" if r["event"] == "points" else clock


def summary(path=None, recent: int = 8, records: int = 8, trend: int = 8) -> dict:
    """Lo que dibuja la pagina HISTORIAL del menu: totales, records por prueba (con la tendencia de sus ultimas marcas,
    de la mas antigua a la mas reciente, ya como tiempo o puntos) y las ultimas carreras. Siempre un dict valido."""
    rs = load(path)
    cats: dict = {}
    for r in rs:
        cats.setdefault(_key(r), []).append(r)
    recs = []
    for k, lst in cats.items():
        best = min(lst, key=_score)
        recs.append({"label": _label(k), "value": _value(best), "date": best["date"][:10], "n": len(lst),
                     "last": lst[-1]["date"], "trend": [r["pts"] if r["event"] == "points" else r["time_s"]
                                                        for r in lst[-trend:]],
                     "lower_better": best["event"] != "points"})
    recs.sort(key=lambda d: d["last"], reverse=True)
    for d in recs:
        d.pop("last")
    last = []
    for r in reversed(rs[-recent:]):
        won = r["event"] == "persecucion" or r.get("pos") == 1
        pos = "" if r["event"] == "persecucion" else f"P{r['pos']}/{r.get('n', 4)}"
        last.append({"date": r["date"], "label": _label(_key(r)), "value": _value(r), "pos": pos,
                     "won": bool(won and r["event"] != "persecucion")})
    km = sum((r["distance_m"] if r["event"] == "persecucion" else r["laps"] * 250) for r in rs) / 1000.0
    return {"n": len(rs), "wins": sum(1 for r in rs if r["event"] != "persecucion" and r.get("pos") == 1),
            "km": round(km, 1), "records": recs[:records], "recent": last}
