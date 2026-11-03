"""Historial de la sesion: muestras a 1 Hz, estadisticas y tiempo/kcal por zona."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .calories import keytel_kcal_per_min
from .zones import zone_for_hr


@dataclass(frozen=True)
class Sample:
    epoch: int      # unix seconds
    iso: str        # timestamp legible
    hr: int
    zone_idx: int   # 0..5
    kcal_per_min: float


class Session:
    def __init__(self, config):
        self.config = config
        self.reset()

    def reset(self) -> None:
        cfg = self.config
        self.samples: list[Sample] = []
        self.start_ts: float = time.time()
        self.total_kcal: float = 0.0
        self.max_hr: int = 0
        self.min_hr: int = 0
        self._sum_hr: int = 0
        self.zone_seconds: list[int] = [0] * 6
        self.zone_kcal: list[float] = [0.0] * 6

    def add(self, hr: int) -> Sample:
        cfg = self.config
        epoch = int(time.time())
        zone = zone_for_hr(hr, cfg.zones(), cfg.computed_hrmax())
        kpm = keytel_kcal_per_min(hr, cfg.age, cfg.weight, cfg.sex)
        s = Sample(epoch=epoch, iso=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch)),
                   hr=hr, zone_idx=zone.idx, kcal_per_min=kpm)
        self.samples.append(s)
        self._sum_hr += hr
        if hr > self.max_hr:
            self.max_hr = hr
        if self.min_hr == 0 or hr < self.min_hr:
            self.min_hr = hr
        self.total_kcal += kpm / 60.0
        self.zone_seconds[zone.idx] += 1
        self.zone_kcal[zone.idx] += kpm / 60.0
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
