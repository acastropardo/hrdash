# AGENTS.md

HRDash: terminal dashboard (Textual) that reads a BLE heart-rate band and a bike
trainer, and writes a Garmin-compatible FIT + CSV on exit. Single Python package
under `src/hrdash/`, console script `hrdash = "hrdash.cli:main"`.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install -p .venv -e .        # editable install is required for the `hrdash` command
```

`pyproject.toml` claims `>=3.10`, but `config.py` imports `tomllib` → **3.11+ is the real floor**.

## Verification (there is no test suite, linter, formatter or typechecker)

Do not add pytest/ruff unless asked. The only automated assertion is `export.validate_fit()`
(FIT round-trip). Run it as the smoke test after touching `ble.py`, `history.py`, `zones.py`,
`calories.py` or `export.py`:

```bash
.venv/bin/hrdash --simulate --duration 3 --export-dir /tmp/hrdash_check
```

The TUI needs a TTY: `hrdash` exits 2 from a non-interactive shell, so visual layout changes
cannot be verified headless. Use `--simulate` for hardware-free demo and `--probe 15 --trainer "TACX"`
for real payload dumps.

Isolate global state when running: every invocation writes `~/.config/hrdash/config.toml`
(or `$XDG_CONFIG_HOME/hrdash/`) — including `last_device` / `last_trainer`. Prefix
`XDG_CONFIG_HOME=/tmp/...` to avoid mutating the user's config, and `--fresh` to ignore what is
already saved.

## Architecture (what is not obvious from filenames)

- `cli.py` forks into four paths: `--scan`, `--probe`, `--duration` (headless), TUI. The TUI path
  also persists the last sensor/trainer on exit.
- `ble.py` sources are async tasks; BLE notify callbacks **write plain attributes** (`latest_hr`,
  `watts`, `cadence`, `speed_kmh`, `dist_m`) that `ui.py` polls at 1 Hz in `tick()`. There is no
  event bus or callback into the UI: a new metric must be added as an attribute on the source **and**
  read in `tick()`, `cmd_headless()` and the drawing code.
- `ui.py` mixes two clock domains: data at 1 Hz (`set_interval(1.0, tick)`) and animation on
  separate 0.1 s timers (heart pulse, gauge needles). Keep them decoupled — do not drive animation
  from the data tick.
- Drawing must never raise: all render code runs through `_safe_draw()`, workers run with
  `exit_on_error=False`, and any drawing exception triggers a `_RESCATE` export. Sampling has to
  survive UI failures.
- The 3D window (`glview.py`) runs in a **separate process**: GLFW needs the main thread, which
  Textual already owns, and `multiprocessing` + `spawn` would re-execute the console entry point
  (the child would boot another dashboard). It uses `subprocess` + one JSON snapshot per line on
  stdin; `GLHost` swallows every failure and the TUI silently falls back to 2D. `cluster.py`
  builds the tablero scene in "panel space" (x right, y up, z toward the driver) and `Mesh.panel()`
  flips it into world XZ; meshes arrive in three layers (static / values at 1 Hz / needles per
  frame) because rebuilding all text geometry every frame blows the 20 ms budget — the strip
  **labels** belong to the static layer, only their **values** to the 1 Hz one.
- `glfw` sizes are **points, not pixels**: `_plan_window()` targets 1920×1080 physical pixels,
  dividing by the monitor `content scale` (Retina ×2 → 960×540 pts) and picking a monitor whose
  work area can host it; the module prints `{"win":[...],"fb":[...]}` on stdout so the chosen
  size can be asserted from tests.
- Terminal.app has **no graphics protocol** (no kitty/sixel/iTerm2 inline): a real image cannot be
  composited inside the TUI, which is why the 3D is a native window. `pyproject` keeps GL deps in
  the optional `gl` extra; core install must keep working without them.

## BLE parsing gotchas

The parsers are hand-rolled binary decoders that must stay tolerant of out-of-spec hardware
(Tacx Satori), not just the GATT spec:

- `parse_cycling_power()` reads the crank block even when flag bit 1 does not declare it.
- `parse_csc()` tries both field orders (revs-first vs time-first) and picks the plausible one.
- `cadence_from_crank()` needs the correct counter modulus: `2**32` for standard Cycling Power,
  `65536` for the Satori 16-bit variant (see `rev_mod` in `TrainerSource._on_cp`). Changing one
  path silently breaks the other.
- CSC is subscribed **only when FTMS is absent** (`TrainerSource._setup`): speed/distance sources
  are intentionally mutually exclusive.
- Characteristic lookup is done by substring on the GATT table (`_find_char`) because CoreBluetooth
  sometimes returns partial or variant UUIDs — do not assume the service layout.

## FIT / export gotchas

- garmin-fit-sdk applies its own scales: `speed` must be written in **m/s** and `distance` in
  **metres**; calories and time in zone are aggregated on the lap and session messages.
- FIT record timestamps must be strictly increasing — `export_fit()` skips duplicates to protect
  the encoder; keep that guard when touching sample ordering.
- Keep `validate_fit()` passing: it compares record count, max HR and max power against the session.
  CSV is written before FIT on purpose (CSV survives encoder failures).
- `exportes/`, `*.fit` and `*.csv` are gitignored session data — never commit them.

## Conventions

- Comments, docstrings and user-facing strings are in Spanish; comments/docstrings are written
  without accents (terminal-font safety), while a few printed strings keep them. Match the existing
  Spanish rather than switching to English.
- Broad `except Exception  # noqa: BLE001` blocks are deliberate robustness (the app must never
  die on a malformed payload or a bad frame); do not "clean them up".
- Commit messages are one-line Spanish describing the observable behaviour, not the file changed.
