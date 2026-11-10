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
5. Estetica de velodromo — **hecha** sobre referencias fotograficas (Calshot y Forest City):
   tablon de pino ambarino con juntas, lineas pintadas UCI, meta al final de la recta, muro
   perimetral, gradas escalonadas, cerchos con lamparas calidas, transicion de peralte suave,
   luz calida de dos fuentes + ambiente hemisferico + niebla, charco de luz horneado por
   vertice y sombra de contacto. Pendiente: veta de tablon por textura (ahora es tono por banda)
   y sombras reales.
6. Camara orbitable con raton/touchpad — **hecha** (arrastre con boton = yaw/tilt, dos dedos en
   horizontal = yaw, clic derecho = centrar). Falta confirmar con raton y trackpad fisicos: los
   callbacks se comprueban inyectando eventos sinteticos, no con el gesto real.
7. Teclas escritas en la ventana 3D — **hecha** (el proceso hijo reenvia la pulsacion a la TUI, que
   es la dueña de los bindings; no se duplica la tabla de acciones). Con modal abierto no se simula,
   coincide con la regla de `p`. Falta confirmar con teclado real.

## Motor de render: hace falta Unity?

**No.** Lo que hacia bajar el nivel no era el motor, eran material, luz y composicion, y todo eso
se hace en moderngl (el pipeline actual) sin dependencias nuevas:

- Lo que se ha ganado sin motor: material de madera con tono por tablon, dos luces calidas +
  ambiente hemisferico + especular, niebla de pabellon, charco de luz horneado por vertice,
  geometria de recinto (muro, gradas, cerchos, lamparas), proporciones reales y encuadre.
- Lo que solo da un motor y hoy no hace falta: PBR, sombras en tiempo real, post-proceso
  (bloom, DOF, tonemapping), importacion de assets y rig animado, y fisica.
- Lo que se puede hacer en moderngl cuando haga falta, sin cambiar de motor: texturas
  procedurales (moderngl admite texturas desde bytes, sin PIL), un shadow map de una luz, y un
  loader de glTF para el atleta de Blender. Es el camino natural del plan.

Unity no encaja con la arquitectura del proyecto: el render es un **proceso hijo** de Python con
una tuberia de JSON a 1 Hz, sin tests ni ventana de control, la TUI es la dueña del teclado, y la
validacion es offscreen. Meter Unity significa un segundo runtime con su propio ciclo, licencia,
empaquetado, y perder la validacion headless y el fallback "la TUI sigue en 2D si el 3D falla".
Si en algun momento se quisiera motor, el criterio seria: solo si se necesitan sombras reales +
post-proceso + assets importados, y entonces como proceso aparte con el mismo protocolo JSON.

## Bug encontrado (fix aplicado)

- `ConfigModal.CSS` usaba `border: round $brand;` -> `$brand` **no existe** en Textual 8.2.8.
  Al presionar `c`, `push_screen(ConfigModal)` hacia `reparse()` y levantaba
  `UnresolvedVariableError` -> la app crasheaba. Cambiado a `border: round #ef4444` (literal,
  como `#err`).
- `p` solo actua si no hay modal abierto (con modal, la tecla va al `Input`).
- Las bandas de tablones y las lineas pintadas se apoyaban en el mismo plano que la capa base:
  el z-buffer elige al azar entre dos poligonos coplanares -> la pista salia con **manchas**
  (z-fighting). Solucion: `lift` de 4..12 mm en `_surf`/`ribbon`. Con color por vertice
  (`vquad`) la gradacion del charco de luz es suave; con un color por cuadro salia en
  "ladrillos" (una linea cada metro de cuerda).
- Las ventanas de GLFW son `c_void_p`: no son hashables (no pueden ser clave de `press`) y cada
  callback recibe su propia instancia, distinta de la que devolvio `create_window` aunque apunten a
  la misma ventana, asi que `is` fallaba en el hit-test. Con una sola vista se sortea por
  `len(mine) == 1` (por eso la rueda funcionaba); con split o con arrastre hay que comparar el
  `value` -> `winkey()`/`same_win()`.
- El click que enfoca la ventana no debe mover la camara: `on_button` solo ancla el punto y el
  gesto empieza en el movimiento siguiente. En MacOS el primer click sobre la ventana suele ser el
  que la enfoca (GLFW no entrega eventos de boton a una ventana sin foco), asi que hay que clicar
  una vez y despues arrastrar.
- El topete se aplica sobre el valor nuevo, no sobre el actual: si el tilt ya estaba en el topete,
  comprobar antes de sumar permitia seguir empujando y el valor se salia (la camara se quedaba
  pegada arriba, sin poder volver).
- El ancla del arrastre se guardaba con `id(ventana)` y se leia con `winkey(ventana)`: son claves
  distintas para un `c_void_p` (el `value` del puntero no es su `id`), asi que el gesto solo
  funcionaba cuando coincidian. Ahora todo el camino del arrastre usa `winkey()`. Ademas, sin el
  `RELEASE` el ancla sobrevivia y cualquier movimiento suelto del cursor seguia orbitando.

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
- Estetica de velodromo (`680e3d2` + paleta recalibrada): paleta **medida de las fotos de
  referencia** (Calshot y Forest City) decodificando el JPEG a BMP con `sips` y promediando bloques
  en Python puro (no hay PIL en el entorno, y la API de muestreo no es accesible desde el sandbox).
  Valores de foto: madera en zona media **#b59b77**, bajo la lampara **#d7c2a9**, muro **#73613a**,
  cupula gris calida #2b1d0b. La primera paleta (madera #9d7143) estaba saturada de mas y el
  pabellon salia naranja; el albedo se pone por encima del valor de foto porque el shader lo
  multiplica por la luz (para un plano horizontal la luz total es ~0,87, asi que albedo
  #bba17e reproduce #a5906f de foto, que es el valor medido en la curva iluminada). Lo que se añadio:
  tablon con junta, lineas UCI en sus distancias reales (azul 0,10 m, negra 0,24, roja 0,94,
  azul del stayer 4,30, borde blanco), meta al final de la recta de meta, muro perimetral con el
  tablon elevado 1,05 m sobre el piso, gradas de 11 filas escalonadas con bloques de color y
  pasillos, cerchos + lamparas calidas, y transicion de peralte con coseno. La luz es dos fuentes
  (calida 0,95 + relleno frio 0,19) + ambiente hemisferico + especular + niebla; el charco de luz
  se hornea por vertice (`tint`) porque el programa no tiene luces puntuales. Paso del trazado
  2 m -> 1 m: con 2 m las bandas estrechas serruchaban en las curvas. Coste: 73.172 tris
  estaticos (se suben una vez) y 730 por frame; medido en el proceso real 47,7 fps / 4,5 ms.
  Comprobacion visual de la recalibracion: `v14_curve.png` (curva), `v14_straight.png` (recta con
  cerchos y lamparas) y `v14_pov.png` (1a persona, manillar y manos en el cuadro).
- Camara en 1a persona separada del chase: ojo a 1,15 m (altura de los ojos, no de la cumbrera
  del casco) y pitch 0,55 rad; con el pitch del chase el manillar y la rueda delantera salian
  fuera del encuadre. El `roll` de la rueda gira por distancia recorrida (arco/radio), no por
  cadencia, asi que los radios se paran cuando para el rider.
- Camara orbitable (fase 6): `STEP_LOOK 0,011` rad/px de arrastre, `STEP_LOOK_SCROLL 0,13`
  rad/unidad de scroll horizontal, `LOOK_SMOOTH 7,0` 1/s de inercia (los eventos de arrastre llegan
  a 60-120 Hz y el frame va a ~50 fps; sin suavizado el gesto sacude el encuadre, se integra con el
  `dt` real del loop), topetes `LOOK_MAX_YAW = pi` / `LOOK_MAX_TILT = 0,45` y `LOOK_MIN_H = 0,15`
  (el ojo no puede quedar por debajo del tablon). En chase gira el rig completo (ojo + mira)
  alrededor del torso del rider, no solo la mira: girando solo la mirada, con el yaw por encima de
  80 grados el rider salia del cuadro y la camara miraba la grada. En POV el arrastre es giro de
  cabeza (el ojo no se mueve: orbitar un ojo a 0,3 m del casco es un circulo de 12 cm). Signos:
  `tilt` positivo sube el ojo en chase y mira arriba en POV. El hijo reporta `look` en el mensaje
  de fps y acepta `snap["look"]` desde fuera, igual que el zoom, por eso se puede probar sin raton.
  Renders con look aplicado: `v15_look.png` (yaw 1,2 / tilt 0,3, el rider sigue en el cuadro),
  `v15_low.png` (tilt -0,45, el guard mantiene el ojo sobre el tablon) y `v15_pov.png` (giro de
  cabeza). El render neutro con look [0,0] es byte-identico a `v14_curve.png` -> no hay regresion
  de encuadre. Hint de la TUI: `r: race (rueda: zoom, arrastre: orbita)`.
- Teclas desde la ventana (`on_key` + `App.simulate_key`): el hijo no tiene acciones propias, solo
  traduce el codigo de GLFW al nombre que la TUI usa (`KEYNAMES`) y reenvia **solo PRESS** (con
  REPEAT la TUI recibiria una accion por cada repeticion del teclado). La TUI lo ejecuta con
  `simulate_key`, y con modal abierto lo ignora (la tecla real iria al `Input`). El foco es el
  condicionante: GLFW solo entrega teclas y clics a la ventana enfocada, y cuando la ventana los
  recibe el terminal deja de recibirlos, asi que el reenvio es el unico camino. Se pide el foco al
  crear la ventana (`focus_window`) y el estado se reporta en el mensaje de fps (`focus`), por eso se
  comprueba sin teclado. En el proceso real la ventana nace sin foco (el script no es la app activa de
  MacOS): hasta clicar la ventana no llega nada, y eso se avisa en la linea de ayuda. El lector de la
  tuberia lee todos los mensajes, no solo el error (`GLHost.drain()`), y los drains a 10 fps: una tecla
  no puede esperar al tick de datos de 1 Hz.

## Proximos pasos

1. Confirmar el zoom con la rueda real en la ventana (el evento de scroll no se puede
   sintetizar headless; el camino de fijar zoom por snapshot si esta probado).
2. Rider en Blender: pipeline de importacion (malla o rig) y su hueco en el presupuesto de
   frame; mientras, el atleta es procedural (cajas + prismas).
3. Input por vista: la rueda ya actua sobre la vista bajo el cursor; si se añaden teclas por
   vista hace falta enfocar la ventana (ahora la TUI es la dueña del teclado).
4. Faltan en la pista: curva con radio variable tipo Monaco (ahora las dos curvas son
   semicirculos de 36 m) y una zona de boxes/entrada, si se quiere que el rider entre y salga
   del trazado. La veta del madera ahora es tono por banda; el siguiente paso es textura
   procedural (moderngl admite texturas desde bytes, sin PIL) y un fillet en la transicion
   plano->peralte, que en las fotos es curvo, no angular.
5. Sombras: hoy es un parche de contacto bajo las ruedas. Si se quieren sombras reales hace
   falta un shadow map de una luz (se puede en moderngl); no hace falta motor.

## Como se verifica

- Harnesses offscreen (fuera del repo, en el directorio de sesiones): `rider_check.py`
  (encuadre y cacheo de capa estatica), `stop_check.py` (inercia y estela), `split_check.py`
  (cada vista en su rectangulo, `far` por vista), `aesthetics_check.py` (before/after de la fase
  5: renderiza `pista` con ventana invisible, escribe PNG sin PIL y reporta tris estaticos y de
  frame; con `WARM=n` repite `scene_pista` para poblar la estela) y `fps_check.py` (proceso real
  con la tuberia abierta para que reporte fps). Dos cosas del harness: `screen.read()` en macOS
  devuelve el framebuffer con content scale 2 (2000x1250 para una ventana de 1000x625) y hay que
  deducir el tamano del propio read; y `readline()` sobre un stdout no bloqueante levanta
  `BlockingIOError` en lugar de devolver
  `None`; hay que capturarla o el script cuelga.
- `look_check.py` (camino del arrastre sin raton real): como los eventos del sistema no se pueden
  sintetizar, se sustituyen los setters de callback por un registro y el harness invoca el callback
  con coordenadas inventadas, con `get_cursor_pos` fijado a un valor conocido (con el valor real el
  ancla del arrastre es unpredictable y el test no se puede afirmar). Cubre seis bloques de 60
  frames, uno por gesto, y lee el `look` que reporta el mensaje de fps de cada bloque: clic sin
  mover -> [0,0]; arrastre -> `STEP_LOOK * delta`; clic derecho -> centrado; scroll horizontal ->
  `STEP_LOOK_SCROLL * dx`; scroll vertical -> zoom; empujar el tilt 100 veces -> se queda en el
  topete. El gesto real no se puede confirmar con un raton o un trackpad fisicos (y el arrastre
  exige que la ventana este enfocada), solo con esta via.
- `key_check.py` (teclas sin teclado real): se registran los setters de callback y el harness invoca
  `on_key` con teclas inventadas. Comprueba que `PRESS` reenvia y `REPEAT` no, que una tecla no ligada
  no emite, que los nombres reenviados estan dentro de los `BINDINGS` de la TUI, y que el foco se pide
  al crear la ventana. `ui_key_check.py` cubre el lado TUI con `run_test()` (sin TTY): tecla reenviada
  ejecutada, ignorada con modal, aviso de sin foco y lector que falla sin tumbar el muestreo. El bloque
  7 de `look_check.py` comprueba el ancla: tras el `RELEASE` un movimiento suelto del cursor no orbita.
- Proceso real: una linea JSON por stdin, mensajes `win`/`err`/`fps` por stdout; se comprueba
  que el layout cambia on the fly, que sale con exit 0 y que no quedan procesos huerfanos.
- TUI: `3` y `r` (toggles independientes), `p` intacto, `x` reset, `c` abre el modal sin crash.
- Smoke del repo: `XDG_CONFIG_HOME=/tmp/hrdash_xdg .venv/bin/hrdash --simulate --duration 3
  --export-dir /tmp/hrdash_check` -> FIT round-trip OK.
