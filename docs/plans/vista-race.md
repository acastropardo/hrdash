# Plan: Vista Pista (race) — render 1 persona estilo NFS + dashboard simultaneo

Estado en `origin/main`: `eff84e6`. Este fichero es la copia **controlada** del plan de
trabajo: se actualiza con cada fase y se commita junto con el codigo que la implementa.
(La copia privada de sesiones queda como espejo; aqui se decide, aqui se consulta.)

## Contexto tecnico

- **Nucleo 100 % portable (Python puro)**: zonas FC y de potencia (`zones.py`), Keytel
  (`calories.py`), sesion 1 Hz (`history.py`), fuente/digitos ASCII (`bigfont.py`), export
  FIT con SDK oficial Garmin + round-trip (`export.py`).
- **Capas NO portables**: BLE con `bleak` (CoreBluetooth/BlueZ) y render ANSI de `textual`
  sobre PTY.
- **3D**: toggle por vista en un proceso aparte (`subprocess` + JSON snapshot por linea sobre
  stdin/stdout; `GLHost` en `ui.py`, escena en `cluster.py`, ventanas en `glview.py`).
- **Vista `pista` en TUI** (`p`): ovalo braille con la bici en la vuelta de `lap_m`.

## Decisiones

1. Tecla `r` (race) = ventana/vista race (pista). Tecla `3` = vista dashboard (cluster).
2. Render simultaneo de ambas vistas (dashboard + race).
3. Multi-monitor: una ventana GLFW por monitor (`glfw.get_monitors`,
   `create_window(monitor, ...)`, contexto por ventana).
4. Fallback: una sola ventana con split vertical — dashboard izquierda 40 %, race derecha 60 %.
5. Toggle independiente por vista; con una sola vista activa, esa ventana es full screen.
6. `reset_session` movido de `r` a `x` (libera `r` para race).

## Fases

1. Geometria de pista 400 m tipo Londres/Monaco (rectas + curvas peraltadas, linea de
   salida/meta, kerbs, asfalto) — **hecha** a escala real (1 unidad = 1 m): 86,9 m de recta
   + 113,1 m de curva, 8 calles, peralte solo en curvas con rampa de entrada/salida.
2. Camara chase + zoom con la rueda (chase <-> primera persona como maximo) — **hecha**
   (rango 0,20..6,0 m, notch 0,18 m, aplicado a la vista bajo el cursor). Falta confirmar con
   la rueda fisica: un evento de scroll no se puede sintetizar headless.
3. Rider = atleta simulado; fase futura: renders Blender — **hecha** en version procedural
   (cuadro, dos ruedas, pedalada con la cadencia, torso, brazos, casco). Pendiente el modelo
   Blender y su pipeline de importacion.
4. Render en vivo con telemetria y parada realista antes de meta — **hecha** (inercia de
   velocidad y cadencia, estela por posiciones reales).

## Bug encontrado (fix aplicado)

- `ConfigModal.CSS` usaba `border: round $brand;` -> `$brand` **no existe** en Textual 8.2.8.
  Al presionar `c`, `push_screen(ConfigModal)` hacia `reparse()` y levantaba
  `UnresolvedVariableError` -> la app crasheaba. Cambiado a `border: round #ef4444` (literal,
  como `#err`).
- `p` solo actua si no hay modal abierto (con modal, la tecla va al `Input`).

## Estado de la implementacion

- `bff0cfe`: camara chase, streak fino sobre el asfalto, rider de placeholder, reset en `x`.
- `4ea8d27`: `gl_views` en la TUI con toggles independientes (`3` gauge, `r` pista), snapshot
  con `views`, y en el proceso hijo una ventana por monitor cuando hay monitores para todas;
  si no, split vertical 40/60; full screen con una sola vista.
- Contexto por ventana: `create_context(share=True)` falla en moderngl 5.13 ("unknown mode")
  -> un contexto con su `program`/`VAO` por ventana. Los window de GLFW no son hashables ->
  deduplicar por `id()` (`_uniq`).
- `swap_interval(0)` en multi-ventana (el vsync se pagaria dos veces): 7,5 ms/frame frente a
  42,9 ms con `swap_interval(1)`.
- Zoom (`2261fdc`, `febe388`): `ZOOM_MIN 0,20` / `ZOOM_MAX 6,0` / `STEP_ZOOM 0,18` (default
  `CHASE_DIST 2,4`), `set_scroll_callback` por ventana y `view_under()` para resolver la vista
  bajo el cursor en el split. La rueda solo afecta a las vistas con camara configurable
  (`ZOOMABLE = ("pista",)`) y el valor se recorta a `ZOOM_MIN..ZOOM_MAX` por rueda y por
  snapshot. En primera persona (`dist < 0,5`) no se dibuja el cuerpo: la camara esta en el
  casco y taparia el cuadro. El proceso reporta `dist` en el mensaje de fps (por eso se puede
  probar sin raton, fijando zoom desde el snapshot). Hint de la TUI: `r: race (rueda: zoom)`.
- Atleta simulado (`63edea0`): dos helpers en `Mesh` — `segment(a, b, r, col)` (prisma de 4
  caras, miembros y tubos del cuadro) y `wheel(c, r, fwd, up, lat, col)` (neumatico como banda
  **cilindrica**: una banda plana con normal lateral se ve de canto desde la chase y
  desaparece). Marco local `(avance, altura, lateral)` apoyado en la superficie peraltada, con
  rodilla por IK de dos tramos (`_knee`) y dos platos en fases opuestas. Manos y pedal se
  dibujan siempre; torso, piernas, brazos y casco solo en chase. La pedalada avanza con la
  cadencia, asi que cuando la telemetria se para el pedaleo se para.
- Pista a escala real (`9586204`, `eff84e6`): `track()` con paso exacto de arco (86,9 m de
  recta + 113,1 m de curva = 400 m), sentido antihorario, meta en el primer punto y `bank`
  **por punto** con rampa (en rectas el peralte es 0). La altura de la banda es
  `tan(bank) * (q + HALF_W)` con `q` la coordenada lateral desde el borde interior: antes era
  `sin`, que no define un plano, y con bank 0 el `up` salia horizontal (la camara quedaba a
  nivel de suelo). `HALF_W = 5,0` (8 calles de 1,22 m), `KERB` rojo en el borde interior,
  lineas de calles extremas, suelo del estadio.
- Inercia: la telemetria no mueve la escena directo, pasa por `anim["v"]` y `anim["c"]` —
  3,0 m/s^2 acelerando, 2,5 frenando, +/-40 rpm/s de cadencia. Con 33,2 km/h y telemetria
  parada: 8,91 m/s -> 7,54 a 0,5 s, 3,78 a 2 s, 0 a 3,6 s, y el rider se queda 29,1 m por
  inercia. La estela se guarda en `anim["trail"]` como posiciones reales con marca de tiempo
  (24 muestras, 1,2 s): al parar deja de crecer y se desvanece, ya no persigue al rider quieto.
  `_at()` interpola entre muestras del trazado (antes saltaba de indice en indice y la estela
  se cortaba).
- Proporciones del atleta en metros: rueda 0,34, pedalier 0,27, ejes -0,53/+0,56, cadera 0,78,
  hombro 1,06, casco 1,22, biela 0,17, muslo 0,43, espinilla 0,44. Camara en metros: eye 1,25
  en 1a persona -> 1,60 en chase, fovy 1,15 -> 1,00, pitch 0,50 -> 0,30 rad (con 0,62 la bici
  quedaba fuera del cuadro a 2,4 m y en POV no se veian manos ni rueda). El `far` de la
  proyeccion viaja como 5º elemento de la camara (300 m; el cluster sigue con 80 y el loop del
  proceso acepta camaras de 4 o 5 elementos).
- Capa estatica: lo que no cambia nunca esta en `build_static()` y se conserva como capa
  cacheada (`None` cuando no cambia, igual que el cluster); el proceso solo sube el VBO de
  `frame` por frame -> 2,9 ms/frame con 2668 tris estaticos + ~300 del rider. `open_windows()`
  crea un `anim` nuevo por slot, asi que al cambiar de layout la capa estatica se reconstruye
  sola.
- Guard de posicion: `abs(anim_frac - snap_frac) > 0,3` **jalaba** al rider al snap y mataba
  la inercia (con telemetria parada la fraccion quedaba fija y la estela en 1 muestra). Ahora
  se detecta que el snap **retrocede** (`(frac - anchor) % 1 > 0,3`) = vuelta cerrada o reset,
  y el resto del tiempo la fraccion avanza con la velocidad suavizada. Medido con `dist_km`
  0,12 y telemetria parada: el rider se queda a 149,1 m de meta y la estela se desvanece hasta
  1 marca.
- El circuito siempre mide 400 m: la posicion del rider es `dist_km*1000 % 400` (el odometro
  real), no `lap_frac`, que sigue usandose solo en la vista `pista` de la TUI.

## Proximos pasos

1. Confirmar el zoom con la rueda real en la ventana (el evento de scroll no se puede
   sintetizar headless; el camino de fijar zoom por snapshot si esta probado).
2. Rider en Blender: pipeline de importacion (malla o rig) y su hueco en el presupuesto de
   frame; mientras, el atleta es procedural (cajas + prismas).
3. Input por vista: la rueda ya actua sobre la vista bajo el cursor; si se añaden teclas por
   vista hace falta enfocar la ventana (ahora la TUI es la dueña del teclado).
4. Faltan en la pista: calles marcadas en el asfalto, curva con radio variable tipo Monaco
   (ahora las dos curvas son semicirculos de 36 m) y una zona de boxes/entrada, si se quiere
   que el rider entre y salga del trazado.

## Como se verifica

- Harnesses offscreen (fuera del repo, en el directorio de sesiones): `rider_check.py`
  (encuadre y cacheo de capa estatica), `stop_check.py` (inercia y estela), `split_check.py`
  (cada vista en su rectangulo, `far` por vista). Nota headless: con `os.set_blocking(False)`
  sobre un stdout en modo texto, `readline()` levanta `BlockingIOError` en lugar de devolver
  `None`; hay que capturarla o el script cuelga.
- Proceso real: una linea JSON por stdin, mensajes `win`/`err`/`fps` por stdout; se comprueba
  que el layout cambia on the fly, que sale con exit 0 y que no quedan procesos huerfanos.
- TUI: `3` y `r` (toggles independientes), `p` intacto, `x` reset, `c` abre el modal sin crash.
- Smoke del repo: `XDG_CONFIG_HOME=/tmp/hrdash_xdg .venv/bin/hrdash --simulate --duration 3
  --export-dir /tmp/hrdash_check` -> FIT round-trip OK.
