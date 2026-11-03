"""Configuracion de HRDash: edad, sexo, peso, zonas, exportacion. Persistencia TOML."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .zones import DEFAULT_ZONE_PCTS, build_zones

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hrdash"
CONFIG_PATH = CONFIG_DIR / "config.toml"


def default_hrmax(age: int) -> int:
    """Tanaka: 208 - 0.7 * edad."""
    return round(208 - 0.7 * age)


@dataclass
class Config:
    age: int = 47
    sex: str = "M"            # M | F
    weight: float = 70.0
    ftp: int = 240             # umbral de potencia funcional (para colorear watts)
    hrmax: int | None = None   # None -> Tanaka por edad
    zone_pcts: list = field(default_factory=lambda: [list(p) for p in DEFAULT_ZONE_PCTS])
    export_dir: str | None = None   # None -> ./exportes
    export_fit: bool = True
    export_csv: bool = True
    wheel_mm: int = 2135         # perimetro de rueda para velocidad via CSC (28" = 2135)
    last_device: str | None = None  # nombre o direccion de la ultima banda
    last_trainer: str | None = None  # nombre o direccion del ultimo rodillo

    def computed_hrmax(self) -> int:
        return self.hrmax if self.hrmax else default_hrmax(self.age)

    def zones(self):
        return build_zones(self.computed_hrmax(), self.zone_pcts)

    def effective_export_dir(self) -> Path:
        if self.export_dir:
            return Path(self.export_dir).expanduser()
        return Path.cwd() / "exportes"


def load_config() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        try:
            data = tomllib.loads(CONFIG_PATH.read_text())
        except Exception:
            return cfg
        for k in ("age", "sex", "weight", "ftp", "hrmax", "export_dir", "wheel_mm",
                  "export_fit", "export_csv", "last_device", "last_trainer"):
            if k in data:
                setattr(cfg, k, data[k])
        if "zone_pcts" in data and len(data["zone_pcts"]) == 5:
            cfg.zone_pcts = [list(p) + [None] * (2 - len(p)) for p in data["zone_pcts"]]
    return cfg


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Configuracion HRDash",
        f"age = {cfg.age}",
        f'sex = "{cfg.sex}"',
        f"weight = {cfg.weight}",
        f"ftp = {cfg.ftp}",
    ]
    if cfg.hrmax:
        lines.append(f"hrmax = {cfg.hrmax}")
    def pct_to_toml(p):
        return f"[{p[0]}, {p[1]}]" if p[1] is not None else f"[{p[0]}]"
    pcts = ", ".join(pct_to_toml(p) for p in cfg.zone_pcts)
    lines.append(f"zone_pcts = [{pcts}]")
    if cfg.export_dir:
        lines.append(f'export_dir = "{cfg.export_dir}"')
    lines.append(f"export_fit = {str(cfg.export_fit).lower()}")
    lines.append(f"export_csv = {str(cfg.export_csv).lower()}")
    lines.append(f"wheel_mm = {cfg.wheel_mm}")
    if cfg.last_device:
        lines.append(f'last_device = "{cfg.last_device}"')
    if cfg.last_trainer:
        lines.append(f'last_trainer = "{cfg.last_trainer}"')
    CONFIG_PATH.write_text("\n".join(lines) + "\n")


def _ask(prompt: str, default: str, cast, validate=None) -> object:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default if not isinstance(default, str) else default
        try:
            val = cast(raw)
        except (ValueError, TypeError):
            print("  Valor no valido, proba de nuevo.")
            continue
        if validate and not validate(val):
            print("  Valor fuera de rango, proba de nuevo.")
            continue
        return val


def prompt_interactive(cfg: Config, non_interactive: bool = False) -> Config:
    """Pregunta edad/sexo/peso al arrancar (defaults: 47 / M / 70)."""
    if non_interactive:
        return cfg
    print("=== HRDash: datos del atleta (Enter = valor por defecto) ===")
    cfg.age = int(_ask("Edad", cfg.age, int, lambda v: 5 <= v <= 120))
    sex = _ask("Sexo (M/F)", cfg.sex, str, lambda v: v.upper() in ("M", "F")).upper()
    cfg.sex = sex
    cfg.weight = float(_ask("Peso en kg", cfg.weight, float, lambda v: 20 <= v <= 400))
    print(f"FCmax estimada (Tanaka): {cfg.computed_hrmax()} bpm")
    return cfg
