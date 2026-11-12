"""Protocolo entre el motor y la ventana 3D: una linea JSON por snapshot, una por mensaje.

El contrato es lo unico que une motor y renderer, y es lo que permite que el stack cambie
sin tocar el nucleo (bleak + zonas + export, 100 % portable):

- Motor -> hijo: un snapshot por linea, a 1 Hz. Claves y tipos en SNAPSHOT_KEYS.
- Hijo -> motor: mensajes por stdout: `win`/`err`/`fps`/`focus`/`key`/`zoom`/`views`.
  `views` solo se reporta en standalone, donde la ventana es la dueña de la vista.
- `GLHost.push` añade `version` a cada snapshot para deduplicar en el harness.

Las capas de escena ya son serializables: `Mesh.v` es una lista plana de floats y
`layers_to_json` la emite como `{"layer": nombre, "v": [...]}` con `None` para la capa
cacheada. Un renderer futuro consume eso sin importar `cluster.py`.

El `version` es el numero de protocolo, no el de la app. Se sube solo si cambia el
esquema; el hijo debe tolerar claves desconocidas (recibe el dict entero, `validate`
solo informa, nunca levanta).
"""

from __future__ import annotations

import json

PROTOCOL = 1

# Orden de recorrido con las flechas, igual en la consola y en standalone (ui.py lo usa
# para `← →` y glview para el ciclo en la ventana).
VIEW_ORDER = ("hr", "watts", "gauge", "pista")

# Claves del snapshot y su tipo esperado (list = lista, "num" = int o float,
# "pair" = pareja de numeros). Las que faltan o vienen con otro tipo se reportan.
SNAPSHOT_KEYS = {
    "view": "str",
    "views": "list",
    "standalone": "bool",
    "hr": "int",
    "watts": "int",
    "cadence": "num",
    "speed": "num",
    "hrmax": "int",
    "ftp": "int",
    "zone_idx": "int",
    "zone_name": "str",
    "zone_color": "str",
    "wband": "int",
    "wzone_color": "str",
    "alarm": "bool",
    "max_hr": "int",
    "avg_hr": "num",
    "pct_max": "num",
    "max_watts": "int",
    "avg_watts": "num",
    "max_speed": "num",
    "avg_speed": "num",
    "max_cadence": "num",
    "avg_cadence": "num",
    "elapsed_s": "int",
    "total_kcal": "num",
    "dist_km": "num",
    "n": "int",
    "zone_secs": "int",
    "kcal_min": "num",
    "wkg": "num",
    "lap_km": "num",
    "age": "int",
    "weight": "num",
    "sex": "str",
    "samples": "list",
    "pzone_kj": "list",
    "pzone_s": "list",
    "zones": "list",
    "bands": "list",
    "zcolors": "list",
    "wcolors": "list",
    "lap_m": "int",
    "lap_frac": "num",
    "zoom_cmd": "list",
    "cam_cmd": "int",
    "phase": "str",
    "hud_cmd": "int",
}

# Mensajes por stdout del hijo. El engine de `cli.cmd_3d` procesa `key` (q/x/s), `views`
# (la ventana elige la vista en standalone) y `zoom`; `err` se lee como estado del host.
ENGINE_KEYS = ("key", "views", "zoom", "err")


def encode(obj) -> str:
    """Una linea JSON (la misma forma que usa la tuberia), con default=float."""
    return json.dumps(obj, default=float)


def decode(line):
    """Linea JSON -> dict; None si la linea no es un objeto (tolerante, nunca levanta)."""
    try:
        got = json.loads(line)
    except Exception:  # noqa: BLE001 - linea malformada: se ignora, la app no muere
        return None
    return got if isinstance(got, dict) else None


def validate(obj) -> list:
    """Problemas de esquema sobre un snapshot: [(clave, problema)]. Vacio = OK.

    No levanta: el harness afirma sobre el informe, y el hijo debe seguir tolerante a
    claves de protocolos mas nuevos (solo informa, nunca rompe el render).
    """
    problems = []
    for k, want in SNAPSHOT_KEYS.items():
        if k not in obj:
            problems.append((k, "falta"))
            continue
        v = obj[k]
        if want == "list":
            if not isinstance(v, list):
                problems.append((k, f"no es lista ({type(v).__name__})"))
        elif want == "num":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                problems.append((k, f"no es numero ({type(v).__name__})"))
        elif want == "bool":
            if not isinstance(v, bool):
                problems.append((k, f"no es bool ({type(v).__name__})"))
        elif want == "int":
            if isinstance(v, bool) or not isinstance(v, int):
                problems.append((k, f"no es int ({type(v).__name__})"))
        elif want == "str":
            if not isinstance(v, str):
                problems.append((k, f"no es str ({type(v).__name__})"))
    zc = obj.get("samples")
    if isinstance(zc, list) and zc and not all(isinstance(s, (list, tuple)) and len(s) == 3
                                               for s in zc):
        problems.append(("samples", "cada muestra no es (hr, watts, zone_idx)"))
    return problems


def layers_to_json(layers) -> list:
    """Capas de `cluster`/`scene` -> lista JSON-serializable.

    Cada capa es `{"layer": nombre, "v": floats}`; la capa cacheada (`None`) se emite
    como `{"layer": nombre, "v": null}` para que un renderer futuro sepa que no cambia.
    La camara viaja como `{"cam": [eye, center, up, fovy, far?]}`.
    """
    out = []
    for name, mesh in layers:
        v = None if mesh is None else mesh.v
        if v is not None:
            v = v.tolist() if hasattr(v, "tolist") else list(v)   # numpy -> floats de JSON
        item = {"layer": name, "v": v}
        fmt = getattr(mesh, "fmt", None)
        if fmt:
            item["fmt"] = fmt                  # p. ej. "deck": otro formato de vertice
        out.append(item)
    return out


def build_snapshot(session, config, laps, hr_source, tr_source,
                   view="hr", views=None, zoom_cmd=(0, 0), standalone=False,
                   cam_cmd=0, hud_cmd=0, phase="running", menu=None, workout=None, paused=False) -> dict:
    """Construye el snapshot. Es el unico builder: lo usan la TUI y el motor, para que
    el esquema no se duplique y el harness valide una sola fuente."""
    hr = int(session.current_hr or (getattr(hr_source, "latest_hr", None) or 0))
    w = int(session.current_watts or ((getattr(tr_source, "watts", None) or 0) if tr_source else 0))
    cad = float(session.current_cadence or ((getattr(tr_source, "cadence", None) or 0.0) if tr_source else 0.0))
    spd = float(session.current_speed or ((getattr(tr_source, "speed_kmh", None) or 0.0) if tr_source else 0.0))
    from .zones import zone_for_hr, power_band, W_COLORS, POWER_BANDS
    zones = config.zones()
    zone = zone_for_hr(hr or 60, zones, config.computed_hrmax())
    band = power_band(w, config.ftp)
    hrmax = int(config.computed_hrmax())
    return {
        "view": view,
        "views": list(views or [view]),
        "standalone": bool(standalone),
        "hr": hr, "watts": w, "cadence": cad, "speed": spd,
        "hrmax": hrmax, "ftp": int(config.ftp),
        "zone_idx": zone.idx, "zone_name": zone.name, "zone_color": zone.color,
        "wband": band, "wzone_color": W_COLORS[band],
        "alarm": bool(hr and hr >= 0.95 * hrmax),
        "max_hr": int(session.max_hr), "avg_hr": float(session.avg_hr), "pct_max": float(session.pct_max),
        "max_watts": int(session.max_watts), "avg_watts": float(session.avg_watts),
        "max_speed": float(session.max_speed), "avg_speed": float(session.avg_speed),
        "max_cadence": float(session.max_cadence), "avg_cadence": float(session.avg_cadence),
        "elapsed_s": int(session.elapsed_s), "total_kcal": float(session.total_kcal),
        "dist_km": float(session.dist_km), "n": int(session.n),
        "zone_secs": int(session.zone_seconds[zone.idx]),
        "kcal_min": 60.0 * session.total_kcal / max(1, session.elapsed_s) if session.n else 0.0,
        "wkg": float(session.wkg),
        "lap_km": float(laps.lap_m / 1000.0),
        "age": int(config.age), "weight": float(config.weight), "sex": config.sex,
        "samples": [(x.hr, x.watts or 0, x.zone_idx) for x in session.window(70)],
        "pzone_kj": [float(x) for x in session.pzone_kj],
        "pzone_s": [int(x) for x in session.pzone_seconds],
        "zones": [(float(z.low_pct), min(float(z.high_pct), 100.0), z.color) for z in zones],
        "bands": [(float(lo), min(float(hi), 900.0), c)
                  for (_nombre, lo, hi), c in zip(POWER_BANDS, W_COLORS)],
        "zcolors": [z.color for z in zones],
        "wcolors": list(W_COLORS),
        "lap_m": int(laps.lap_m), "lap_frac": float(laps.frac),
        "zoom_cmd": list(zoom_cmd),
        "cam_cmd": int(cam_cmd),
        "hud_cmd": int(hud_cmd),
        "phase": phase,                      # "menu" (antes de Play) o "running"
        **({"menu": menu} if menu is not None else {}),   # estado del menu: solo en fase menu
        **({"paused": True} if paused else {}),        # sesion en pausa (espacio): clave solo si lo esta
        **({"workout": workout} if workout is not None else {}),   # plan en curso (Engine.plan_info)
    }
