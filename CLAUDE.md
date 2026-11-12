# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

The project guide above (`AGENTS.md`) is the canonical one: setup, the smoke test, BLE/FIT
gotchas and conventions live there. What follows is what it does not cover yet.

## Default mode: 3D standalone; `--ascii` for the TUI

Plain `hrdash` (or `--3d`) runs `cmd_3d`: engine + GLFW window, no TUI. `--ascii` is the
Textual console. `--3d`, `--ascii`, `--scan`, `--probe` and `--duration` are one mutually
exclusive argparse group; `--simulate` must stay **outside** it so it combines with any of
them (it broke once; the smoke test catches it). `--3d-view VISTA` (repeatable, dest read
with `getattr`, default `gauge`) picks the initial views; in the window `left`/`right`
rotate through `proto.VIEW_ORDER` (`standalone_key`). Only the implicit default falls
back to the TUI when `gl_unavailable()` says so; explicit `--3d` fails instead.

`cmd_3d` must catch `asyncio.CancelledError` as well as `KeyboardInterrupt`: since Python
3.11 `asyncio.run` turns Ctrl+C into a task cancellation, and without that the session was
never exported.

```bash
XDG_CONFIG_HOME=/tmp/hrdash_xdg .venv/bin/hrdash --simulate --3d-view gauge   # needs the [gl] extra and a display
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
restore viewport and scissor after it.

Frame loop rules (the mixed gauge+pista view broke both): `swap_buffers` happens **once per
window after all its slots are drawn** (split views share one window; swapping per slot meant two
vsync waits and a half-drawn frame), and the loop must not sleep past ~16 ms (it would put 50 fps
on a 60 Hz display). `cluster.dial_frame` caches the tick marks (`anim["tick_cache"]`); anything
new that is rebuilt per frame in pure Python must fit the ~16 ms budget together with the other
slot (measured: gauge ~3 ms, pista ~7 ms). Plans and
offscreen-render checks for this work are in `docs/plans/velodromo-render.md`.

## Dial text layout

Texts inside a dial (`cluster.dial_static` / `dial_values`) are laid out in radius units and
fitted by `_fit`/`_box_ok` so they stay inside the face and out of the tick ring; centre text
with `draw_text(align="c")` (ink width) and digits with `digits_extent`, never with
`text_w`/`digits_w` (they include trailing gaps and blank cells). Keep dial text at z = 0.06:
a higher z shifts it under perspective in the bottom dials.

## Pre-session menu (phase 1 of `docs/plans/menu-entrenamiento.md`)

`Engine(menu=True)` starts in `phase = "menu"`: sources connect and are readable (`menu_info`) but
`poll()` records nothing until `begin()` (new `Session`/`LapTracker`, simulator clock and trainer
distance reset). The snapshot carries `phase` and, only then, `menu`. The child draws `menuview`
(HUD layers, ortho camera) and answers with `{"ui": {"act": id}}`; widget layout is the pure
`menu_widgets(menu, aspect)`, shared by drawing and hit-testing. In the menu phase the window
must ignore every view key (otherwise `g`/arrows bypass Play); `cmd_3d` polls at 10 Hz there.
`winkey()` must use the pointer address (`ctypes.cast(win, c_void_p).value`): glfw-python's window
is `LP__GLFWwindow` with no `.value`, and `id(win)` differs per callback, which silently broke
`slot_under()` (wheel, drag-orbit, focus, menu hover). Test input with XTest through `ctypes`
(`libXtst`) under Xvfb: real keys and pointer events, no `xdotool` needed. A background `&`
process in a non-interactive script ignores SIGINT: close the app with `q`, not `kill -INT`.

## Workout plans

`workouts.py` is the pure model for the pre-session menu feature (plan:
`docs/plans/menu-entrenamiento.md`): `Block`/`Repeat` -> `Workout`, `at(t)` for "which block am I
in", `resolve_target` (power if present, else HR, else stopwatch only), presets, and
`to_dict`/`to_toml_inline` for `config.toml` and the window protocol. No UI/BLE imports; keep it
that way so it stays checkable headless. `Repeat.between` goes *between* repetitions, not after
the last one.

## Other drawing modules

`pixels.py` is the shared braille framebuffer (2×4 px per cell, z-ordered) used by
`gauges.py` and `track.py`; both cache the background per geometry and redraw only the
moving piece per frame. `laps.py` splits distance into `lap_m` laps for the pista view
(distance is integrated from speed and flagged `estimada` when the trainer sends none);
laps are display-only — the FIT still records a single lap.
