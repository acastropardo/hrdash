# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

The project guide above (`AGENTS.md`) is the canonical one: setup, the smoke test, BLE/FIT
gotchas and conventions live there. What follows is what it does not cover yet.

## Fifth CLI path: `--3d` standalone

`AGENTS.md` describes four branches in `cli.main()`; there are five. `--3d` is in the same
mutually exclusive argparse group as `--scan`, `--probe` and `--duration`: engine + GLFW
window, no TUI. `--simulate` must stay **outside** that group so it can combine with
`--duration` and `--3d` (it broke once; the smoke test catches it). `--3d-view VISTA`
(repeatable, dest read with `getattr`) picks the initial views.

```bash
XDG_CONFIG_HOME=/tmp/hrdash_xdg .venv/bin/hrdash --simulate --3d --3d-view gauge   # needs the [gl] extra and a display
```

## Engine / protocol split

- `engine.py` is the UI-free session core (sources + `Session` + `LapTracker` + export).
  `cmd_headless` (`--duration`) and `cmd_3d` both run on it; it does the same 1 Hz attribute
  poll as `ui.py`'s `tick()`. So a new metric now also has to be read in `Engine.poll()`.
- `proto.py` is the only contract between engine/TUI and the 3D child: one JSON snapshot
  per line (`SNAPSHOT_KEYS`, `validate`, `encode`/`decode`, the shared `build_snapshot`
  used by both TUI and engine). Child → parent messages: `win`/`err`/`fps`/`focus`/`key`/
  `zoom`/`views`. `validate` only reports, never raises; the child must tolerate unknown
  keys. Bump `PROTOCOL` only when the schema changes.
- `proto.VIEW_ORDER` is the single source for arrow-key view cycling, in both the TUI and
  the 3D window.
- Key ownership in standalone: the window handles view/zoom keys locally and reports
  `views`; session keys (`q`, `x`, `s`) are forwarded to the engine. With the TUI,
  `ui.py` owns all bindings.
- In `glview.py` the primary window and its moderngl context must **persist** across view
  changes: a second `create_context` fails on Linux and kills the child (see
  `docs/plans/3d-standalone.md`, "Bug corregido"). Fake-glfw harnesses do not catch this.
- If the window never opens (`err` before `win`), `cmd_3d` exits 2 — but still exports.

## Pista deck layer

`scene_pista` returns a `rider` layer (`ArrMesh`: numpy array in `VATTR` format; rigid parts cached per zone colour in local coords, only legs/cranks/wheel marks rebuilt per frame, numpy imported lazily so core install still works) and a `deck` layer (`DeckMesh`, `fmt = "deck"`) with its own vertex
format (`DECK_VATTR`: position, normal, track coords `(s, q)`, baked lamp warmth) and its own
program (`DECK_VS` / `_deck_fs()`): planks, joints and the longitudinal painted lines are
procedural in the fragment shader, filtered with `fwidth`. `_upload()` picks program and
format per layer; if the deck program fails to compile it falls back to `to_mesh()` (plain
wood, general program). Lighting is shared through the `SHADE` GLSL snippet. Custom vertex
formats live in `FORMATS` (`deck`, and `shadow`, drawn by `_draw()` with multiplicative
blending); add a new one there rather than special-casing the render loop. Camera modes
(`CAM_MODES`, key `v`) are child-process state: the TUI only sends a changing `cam_cmd`
counter, like `zoom_cmd`. The pista also returns `hud*` layers (`cluster.hud_layers`, the same
dials as the gauge cluster) that `_draw()` renders in a second, orthographic pass after
clearing only depth; the camera tuple carries a 6th field (HUD visible, key `i`/`hud_cmd`).
In moderngl 5.13 `fbo.color_mask` only reaches GL on `fbo.use()`, which resets the viewport:
restore viewport and scissor after it. Plans and
offscreen-render checks for this work are in `docs/plans/velodromo-render.md`.

## Other drawing modules

`pixels.py` is the shared braille framebuffer (2×4 px per cell, z-ordered) used by
`gauges.py` and `track.py`; both cache the background per geometry and redraw only the
moving piece per frame. `laps.py` splits distance into `lap_m` laps for the pista view
(distance is integrated from speed and flagged `estimada` when the trainer sends none);
laps are display-only — the FIT still records a single lap.
