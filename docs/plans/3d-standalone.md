# Plan: --3d standalone (motor separado de la consola)

Estado en `origin/main`: `e4c7d46`. Este fichero es la copia **controlada** del plan de
trabajo: se actualiza con cada fase y se commita junto con el codigo que la implementa.

## Contexto

El 3D era un **hijo de la TUI**: `GLHost` arrancado con las teclas `3`/`r`, las teclas
escritas en la ventana se reenviaban al terminal (`simulate_key`), y la escena dependia
del estado de `ui.py`. Eso colisionaba: la ventana y el terminal se disputan el teclado,
y cerrar la ventana o un fallo GL dispara logica de la TUI.

`--3d` es ahora una **quinta rama de `main()`**, excluyente con `--scan`, `--probe` y
`--duration`: motor + ventana, sin TUI. Las teclas de la ventana no colisionan porque no
hay terminal al que reenviar. La TUI y su tecla `3` quedan intactas; `--simulate` sigue
siendo la fuente (no una ruta) y se puede combinar con `--3d`.

## Decisiones

1. `--3d` standalone: la ventana es la interfaz. El terminal se queda quieto y solo
   imprime el resumen al salir (sin TTY: el motor corre igual, la ventana la pone el
   proceso hijo).
2. Motor (`engine.py`) sin UI: fuentes + `Session` + `LapTracker` + export. Es el mismo
   poll de 1 Hz que hace la TUI en `tick()`. Lo usan `cmd_headless` (refactor, output
   identico) y `cmd_3d`.
3. Protocolo (`proto.py`) como unica union: snapshot por linea con esquema documentado
   (`SNAPSHOT_KEYS`, `validate`, `encode`/`decode`) y builder unico compartido por la TUI
   y el motor. Las capas de escena son serializables (`layers_to_json`) por si algun dia
   hay otro backend. Version de protocolo: `PROTOCOL = 1`.
4. Reparto de teclas en standalone: la ventana gestiona localmente la vista (`h/w/g/p`,
   `3`/`r` y `left`/`right`, que recorren el mismo orden que la consola) y el zoom (`+`/`-`
   y la rueda); lo que afecta a la sesion (`q`, `x`, `s`) se reenvia y lo procesa el
   motor. Con TUI la dueña de los bindings sigue siendo `ui.py`. El orden de recorrido es
   `proto.VIEW_ORDER` (fuente unica: la TUI lo usa para `← →` y `standalone_key` para la
   ventana). Con varias vistas abiertas, la flecha cambia la primera sin perder las demas.
   `[ ]` no hace nada en standalone: el largo de vuelta se fija con `--lap` al arrancar.
5. Robustez como hoy: si la ventana cae en marcha, el motor sigue muestreando y exporta
   al salir (CSV antes que FIT). Si la ventana **no abre nunca** (`err` antes de `win`),
   la ruta no tiene sentido y cmd_3d avisa y sale con 2, sin tocar la TUI.
6. Stack: se mantiene moderngl + GLFW (Linux/Windows/macOS). La Fase web/Android no se
   hace; `proto.py` deja la puerta abierta sin coste.

## Estado de la implementacion

- Motor: `Engine` con `start/stop/poll/snapshot/export` y `handle_key` (q/x/s). El poll
  lee los atributos que escriben los callbacks BLE (`latest_hr`, `watts`, `cadence`,
  `speed_kmh`, `dist_m`), sin importar textual.
- `cmd_3d`: loop de 1 Hz, drain de mensajes del hijo (`key`, `views`, `err`), guard de
  ventana no-abierta (exit 2), Ctrl+C graba. `--3d-view` (dest `3d_view`, se lee con
  `getattr`) elige las vistas iniciales.
- Hijo (`child_main`): `standalone` y `local_views` compartidos entre el loop y `on_key`;
  `standalone_key` es funcion pura para poder afirmarla headless. En standalone la ventana
  hereda las vistas del primer snapshot y luego las cambia ella, reportando `views` al
  motor; el motor las refleja en el siguiente snapshot (no se pisaban).
- Persistencia: `cmd_3d` guarda `last_device`/`last_trainer` al salir, igual que la TUI.

## Como se verifica

- Harnesses offscreen (fuera del repo, en el directorio de sesiones):
  - `proto_check.py`: esquema del snapshot sobre un Session real (45 claves), round-trip
    linea por linea, decodificacion tolerante, capas serializables, y la tuberia con un
    dummy hijo que usa `glview._stdin_feed` sin GLFW (3 lineas validadas por version, EOF
    saca al hijo). El dummy responde solo cuando la version cambia: `feed()` devuelve el
    ultimo snapshot en cada llamada, igual que el loop apretado del hijo real.
  - `standalone_check.py`: `cmd_3d` con host stub (misma interfaz que `GLHost`) headless y
    sin TTY. Cubre: snapshots validados con `standalone=True`, teclas `q` (sale y graba),
    `x` (resetea Session+LapTracker) y `s` (reconecta), el mensaje `views` desde la
    ventana, ventana caida en marcha (sigue muestreando y exporta con 0), ventana que no
    abre (exit 2 pero exporta), host que no arranca (exit 2), exclusividad argparse, y
    `standalone_key` (h/w/g/p cambian, 3/r toggle, left/right en el orden de la consola,
    q/x se reenvian). El export se valida con el round-trip FIT (`validate`) en todos.
  - Proceso real (maquina con display): una linea JSON con `standalone` y dos vistas ->
    `win` con las dos vistas, EOF -> exit 0, sin huerfanos. La ventana no se puede
    afirmar headless.
- Falta confirmar con teclado real en la ventana: `h/w/g/p` y `3`/`r` de la ventana
  (hacen falta eventos reales y foco; los nombres se prueban por la funcion pura).
- TUI intacta: `3` y `r` siguen funcionando por el mismo builder (`proto.build_snapshot`,
  `standalone=False`); `import hrdash.ui` sin TTY no rompe.
- Smoke del repo: `XDG_CONFIG_HOME=/tmp/hrdash_xdg .venv/bin/hrdash --simulate --duration 3
  --export-dir /tmp/hrdash_check` -> FIT round-trip OK (ahora sobre el motor).
  Ojo: `--simulate` debe quedar fuera del grupo excluyente, si entra deja de poder
  combinarse con `--duration` (lo rompio una vez durante la fase y el smoke lo detecta).

## Proximos pasos

1. Comprobar `--3d` con ventana real y teclado real: foco, `h/w/g/p`, `q` desde la ventana.
2. Si algun dia se retoma Android: backend web sobre el mismo protocolo (`proto.py`), sin
   tocar el motor. Descartado por ahora.
