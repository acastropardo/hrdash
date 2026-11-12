"""Historial de la sesion: muestras a 1 Hz (FC + telemetria), estadisticas y zonas."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .calories import keytel_kcal_per_min
from .zones import zone_for_hr, power_band


@dataclass(frozen=True)
class Sample:
    epoch: int        # unix seconds
    iso: str
    hr: int
    zone_idx: int     # 0..5
    kcal_per_min: float
    watts: int | None = None
    cadence: float | None = None
    speed_kmh: float | None = None
    dist_m: int | None = None      # distancia acumulada del rodillo
    bloque: str | None = None      # bloque del plan en curso ("Esfuerzo 2/4"); None sin plan


class Session:
    def __init__(self, config):
        self.config = config
        self.reset()

    def reset(self) -> None:
        cfg = self.config
        self.samples: list[Sample] = []
        self.start_ts: float = time.time()
        self.total_kcal: float = 0.0
        self.max_hr = self.min_hr = 0
        self._sum_hr = 0
        self.zone_seconds: list[int] = [0] * 6
        self.zone_kcal: list[float] = [0.0] * 6
        self.pzone_seconds: list[int] = [0] * 6   # banda de potencia 0..5 (%FTP)
        self.pzone_kj: list[float] = [0.0] * 6
        # telemetria del rodillo: (suma, max, min, n) para promedios sobre muestras validas
        self._t = {"watts": [0.0, 0, 0, 0],
                   "cadence": [0.0, 0.0, 0.0, 0],
                   "speed_kmh": [0.0, 0.0, 0.0, 0]}
        self.total_dist_m: int = 0

    def add(self, hr: int, watts: int | None = None, cadence: float | None = None,
            speed_kmh: float | None = None, dist_m: int | None = None,
            bloque: str | None = None) -> Sample:
        cfg = self.config
        epoch = int(time.time())
        zone = zone_for_hr(hr, cfg.zones(), cfg.computed_hrmax())
        kpm = keytel_kcal_per_min(hr, cfg.age, cfg.weight, cfg.sex)
        s = Sample(epoch=epoch,
                   iso=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch)),
                   hr=hr, zone_idx=zone.idx, kcal_per_min=kpm,
                   watts=watts, cadence=cadence, speed_kmh=speed_kmh, dist_m=dist_m,
                   bloque=bloque)
        self.samples.append(s)
        self._sum_hr += hr
        if hr > self.max_hr:
            self.max_hr = hr
        if self.min_hr == 0 or hr < self.min_hr:
            self.min_hr = hr
        self.total_kcal += kpm / 60.0
        self.zone_seconds[zone.idx] += 1
        self.zone_kcal[zone.idx] += kpm / 60.0
        if watts and watts > 0:
            pb = power_band(watts, cfg.ftp)
            self.pzone_seconds[pb] += 1
            self.pzone_kj[pb] += watts / 1000.0     # kJ de trabajo
        for key, val in (("watts", watts), ("cadence", cadence), ("speed_kmh", speed_kmh)):
            if val is not None and val > 0:
                acc = self._t[key]
                acc[0] += val
                acc[1] = max(acc[1], val)
                acc[2] = val if acc[2] == 0 else min(acc[2], val)
                acc[3] += 1
        if dist_m is not None and dist_m > self.total_dist_m:
            self.total_dist_m = dist_m
        return s

    # ---- estadisticas ----
    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def avg_hr(self) -> float:
        return self._sum_hr / self.n if self.n else 0.0

    @property
    def current_hr(self) -> int:
        return self.samples[-1].hr if self.samples else 0

    @property
    def pct_max(self) -> float:
        return 100.0 * self.current_hr / max(1, self.config.computed_hrmax())

    @property
    def elapsed_s(self) -> int:
        return max(0, int(time.time() - self.start_ts))

    def window(self, n: int) -> list[Sample]:
        return self.samples[-n:] if n < self.n else list(self.samples)

    def _tstat(self, key: str, idx: int) -> float:
        acc = self._t[key]
        if not acc[3]:
            return 0.0
        if idx == 0:
            return acc[0] / acc[3]
        return acc[idx]

    @property
    def avg_watts(self) -> float:
        return self._tstat("watts", 0)

    @property
    def max_watts(self) -> int:
        return int(self._tstat("watts", 1))

    @property
    def current_watts(self) -> int:
        for s in reversed(self.samples):
            if s.watts:
                return s.watts
        return 0

    @property
    def avg_cadence(self) -> float:
        return self._tstat("cadence", 0)

    @property
    def max_cadence(self) -> float:
        return self._tstat("cadence", 1)

    @property
    def current_cadence(self) -> float:
        for s in reversed(self.samples):
            if s.cadence:
                return s.cadence
        return 0.0

    @property
    def avg_speed(self) -> float:
        return self._tstat("speed_kmh", 0)

    @property
    def max_speed(self) -> float:
        return self._tstat("speed_kmh", 1)

    @property
    def current_speed(self) -> float:
        for s in reversed(self.samples):
            if s.speed_kmh:
                return s.speed_kmh
        return 0.0

    @property
    def dist_km(self) -> float:
        return self.total_dist_m / 1000.0

    @property
    def wkg(self) -> float:
        return self.current_watts / max(1.0, self.config.weight)
