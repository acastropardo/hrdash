"""Zonas de entrenamiento de FC, con limites en % de FCmax y colores estilo Garmin."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Zone:
    idx: int          # 0..5 (0 = por debajo de Z1)
    key: str          # "Z1".."Z5", "Z0"
    name: str         # nombre legible
    low_pct: float    # % de FCmax (0..100)
    high_pct: float
    color: str        # hex truecolor


# Z0: reposo / por debajo de umbral
ZONES: list[Zone] = [
    Zone(0, "Z0", "Reposo",        0.0,  50.0, "#6b7280"),
    Zone(1, "Z1", "Recuperacion", 50.0,  60.0, "#2f7bff"),
    Zone(2, "Z2", "Aerobico",     60.0,  70.0, "#22c55e"),
    Zone(3, "Z3", "Tempo",        70.0,  80.0, "#facc15"),
    Zone(4, "Z4", "Umbral",       80.0,  90.0, "#f97316"),
    Zone(5, "Z5", "Maximo",       90.0, 999.0, "#ef4444"),
]

# Los limites porcentuales editables son Z1..Z5 (Z0 es el complemento)
DEFAULT_ZONE_PCTS = [(50, 60), (60, 70), (70, 80), (80, 90), (90, None)]

ZONE_NAMES = ["Recuperacion", "Aerobico", "Tempo", "Umbral", "Maximo"]
ZONE_COLORS = ["#2f7bff", "#22c55e", "#facc15", "#f97316", "#ef4444"]


def build_zones(hrmax: int, pcts: list | None = None) -> list[Zone]:
    """Construye las 6 zonas (Z0..Z5) dados la FCmax y los cortes en %.

    `pcts`: lista de 5 pares [low, high] en %. El ultimo high puede ser None (sin tope).
    """
    if pcts is None:
        pcts = DEFAULT_ZONE_PCTS
    out = [Zone(0, "Z0", "Reposo", 0.0, float(pcts[0][0]), "#6b7280")]
    for i, pair in enumerate(pcts):
        low = float(pair[0])
        high = float(pair[1]) if pair[1] is not None else 999.0
        out.append(Zone(i + 1, f"Z{i+1}", ZONE_NAMES[i], low, high, ZONE_COLORS[i]))
    return out


def zone_for_hr(hr: int, zones: list[Zone], hrmax: int) -> Zone:
    pct = 100.0 * hr / max(1, hrmax)
    for z in zones:
        if z.low_pct <= pct < z.high_pct:
            return z
    return zones[-1]


# ---- zonas de potencia (bandas por %FTP, mismos colores que el grafico de W) ----
W_COLORS = ["#6b7280", "#2f7bff", "#22c55e", "#facc15", "#f97316", "#ef4444"]
# (nombre, %FTP bajo, %FTP alto) por banda 0..5
POWER_BANDS = [
    ("P0 Sub-56",  0.0,   56.0),
    ("P1 Rec",    56.0,   76.0),
    ("P2 Aer",    76.0,   91.0),
    ("P3 Tempo",  91.0,  106.0),
    ("P4 Umbral", 106.0, 121.0),
    ("P5 Max",   121.0, 999.0),
]


def power_band(watts: int, ftp: int) -> int:
    """Banda 0..5 segun %FTP: <56, <76, <91, <106, <121, >=121."""
    if not ftp or not watts:
        return 0
    p = 100.0 * watts / ftp
    for i, (_n, lo, hi) in enumerate(POWER_BANDS):
        if lo <= p < hi:
            return i
    return 5
