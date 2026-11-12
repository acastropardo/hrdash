# Plan: menu previo (sensores + sesion por intervalos) y arranque con Play

Estado de partida: `f38d76d` (en `main`). Este fichero es la copia **controlada** del plan: se
actualiza con cada fase y se commitea junto con el codigo que la implementa.

## Lo pedido

1. Un menu **antes** de arrancar, hecho en OpenGL (misma ventana), operable con **raton y/o
   teclado**.
2. En el menu: **detectar y asignar sensores** (banda de FC y rodillo).
3. El render (tablero/pista) **solo arranca con Play** (o boton Grabar), no antes.
4. El menu **pregunta los minutos** y si hay **ejercicio por intervalos** (ej. noruego 4x4, o
   "5 min suave, 3 fuerte, 5 suave..."), con **protocolos habituales** como presets
   (calentamiento de 15 min, sesiones de 30, 45...).

## Como esta hoy (lo que condiciona el diseno)

- `cli.main()` crea las fuentes (`SensorSource`/`TrainerSource`) y `cmd_3d` las arranca de
  inmediato: no hay paso previo. Cada fuente resuelve su dispositivo sola (`_resolve`: MAC/UUID,
  o escanea y toma el que coincide con `last_device`/`last_trainer`, o el primero).
- `Engine` crea `Session` y `LapTracker` en el constructor y `cmd_3d` muestrea a 1 Hz desde el
  primer segundo: no existe "antes de grabar".
- `ble.scan_all()` ya clasifica el escaneo en (bandas FC, rodillos) con nombre/direccion/RSSI.
- La ventana (`glview.py`) es un proceso hijo que solo dibuja: ya tiene callbacks de **teclado,
  clic, cursor y rueda**; el protocolo (`proto.py`) es snapshot por linea hacia el hijo y
  mensajes (`key`, `views`, `cam`...) de vuelta. Los textos se dibujan con la fuente pixel de
  `cluster.py` (sin atlas), los diales con `dial_static/values/frame` y el HUD con la camara
  ortografica `hud_cam`.
- No hay entrada de texto (no hay `set_char_callback`): el menu se maneja con presets, +/- y
  listas, no con campos de escribir.

## Investigacion: protocolos de intervalos habituales

Fuentes: ver al final. Las intensidades se expresan en %FTP (potencia) y %FCmax; el repo ya
tiene esas dos escalas (`zones.py`): zonas FC Z1-Z5 (50-60, 60-70, 70-80, 80-90, 90-100 %FCmax)
y bandas de potencia P0-P5 (<56, 56-76, 76-91, 91-106, 106-121, >121 %FTP).

| Nivel (Coggan) | %FTP | %FCmax aprox. | Banda/zona del repo |
|---|---|---|---|
| Recuperacion activa | < 55 | 50-60 | P0 / Z1 |
| Resistencia | 56-75 | 60-70 | P1 / Z2 |
| Tempo | 76-87 (o hasta 90) | 70-80 | P2 / Z3 |
| Sweet spot | 88-94 | ~80-85 | P2-P3 / Z3-Z4 |
| Umbral | 95-105 | 85-92 | P3 / Z4 |
| VO2max | 106-120 (esfuerzos de 3-8 min) | 90-100 | P4 / Z5 |
| Anaerobico | 121-150 | (la FC no llega) | P5 |

Calentamiento: 10-15 min (hasta 20 en test de FTP); enfriamiento: 5-10 min. El 80/20
"polarizado" es un reparto **semanal**, no una regla por sesion.

Presets propuestos (todos editables; los tiempos suman exactamente el total):

| Preset | Estructura | Total |
|---|---|---|
| Calentamiento | 5' Z1 + 7' Z2 + 3' Z3 progresivo, sin esfuerzo duro | 15' |
| Resistencia | 5' P0-P1, 20' P1 (Z2), 5' enfriar | 30' |
| Sweet spot | 11' calent., 2 x 12' a 88-94 %FTP con 5' rec., 5' enfriar | 45' |
| Noruego 4x4 | 10' calent. a 60-70 %FCmax, 4 x (4' a 90-95 %FCmax, 3' rec. a 60-70 %), 5' enfriar | 40' |
| VO2max 5x3 | 10' calent., 5 x (3' a 106-120 %FTP) con 3' rec., 8' enfriar | 45' |
| 30/30 | 10' calent., 2 series de 6 x (30" fuerte / 30" suave) con 4' entre series, 4' enfriar | 30' |
| Tabata 20/10 | 10' calent., 8 x (20" maximo / 10" suave), 6' enfriar | 20' |
| Umbral 2x20 | 10' calent., 2 x 20' a 95-105 %FTP con 5' rec., 5' enfriar | 60' |
| Libre | N minutos, sin objetivos, solo cronometro | N |
| Personalizado | calentamiento + N x (trabajo T1 / recuperacion T2) + enfriamiento, o lista de bloques | -- |

Notas honestas sobre las fuentes: el noruego 4x4 de Helgerud es 4 x 4' a 90-95 %FCmax con 3'
de recuperacion activa, pero hay fuentes que dan 85-95 % y recuperacion a 60-70 % o a 70 %;
calentamiento y enfriamiento de 10' son practica comun, no parte del estudio original. Para
**Tabata y 30/30** las busquedas no devolvieron la especificacion (los valores salen de la
literatura general: Tabata original 20" a ~170 % del VO2max / 10" de pausa x 8); confirmar antes
de darlos por canonicos. En intervalos cortos (30/30, Tabata) la FC va 1-2 minutos por detras:
el objetivo se da en **potencia** (o solo cronometro si no hay rodillo), no en FC.

## Diseno

**Ventana unica, escena `menu`.** `hrdash` abre la ventana en el menu; Play inicia sesion y
pasa a la vista elegida (por defecto el tablero). La escena es una vista mas del hijo
(`SCENES["menu"]`), dibujada en espacio de pantalla con la camara ortografica del HUD y la
fuente pixel; los botones y listas son cajas con foco/hover.

**Quien es dueno de que.**

- El **padre** (motor) es dueno de los datos: escaneo BLE, dispositivos encontrados, asignacion,
  fuentes conectadas, valores en vivo, la sesion y el plan de entrenamiento. Manda al hijo un
  bloque `menu` en el snapshot (listas, seleccion, estado de cada slot, plan editado).
- El **hijo** es dueno de la interaccion: foco, hover, hit-test (clic y teclas) y animacion.
  Devuelve eventos semanticos `{"ui": {"act": "scan" | "pick" | "set" | "play" | ...}}`; no
  conoce BLE.
- Claves nuevas **opcionales** en el snapshot (`phase`, `menu`, `workout`): el hijo ya tolera
  claves desconocidas, asi que `PROTOCOL` no sube.

**Motor.** `Engine` separa `connect()` (arrancar fuentes, ya existe como `start`) de
`begin()` (crear `Session`/`LapTracker` y arrancar el reloj del plan) y tiene `phase`
(`menu` | `running`). En el menu se leen los valores en vivo pero **no se anade muestra**: nada
se graba antes de Play.

**Sensores.** Boton Buscar (escaneo de 6-8 s, repetible, serializado: BlueZ no admite dos
escaneos a la vez); lista con nombre, RSSI y tipo (FC / rodillo con potencia, FTMS o CSC);
asignar a slot FC o Rodillo con clic o Enter; entradas fijas "Simulador" y "Sin rodillo".
Cada slot muestra estado (buscando / conectando / conectado / error) y el **valor en vivo**
(FC, W, rpm, km/h) para comprobar que funciona antes de iniciar. Al abrir se reconecta el
ultimo usado (`last_device`/`last_trainer`) y se persiste lo que se elija.

**Sesion.** Minutos totales (presets 15/30/45/60/90 y +/-1, +/-5), tipo (Libre, presets de la
tabla, Personalizado) y, para Personalizado, un editor de bloques por botones (tipo, duracion
mm:ss, objetivo, repeticiones). Resumen con perfil de bloques coloreado y total; Play se
deshabilita si el plan no suma o falta la FC.

**Durante la sesion.** Un `WorkoutRunner` (puro, sin UI) traduce tiempo transcurrido a
(bloque, restante, siguiente, progreso); el snapshot lleva `workout`. El HUD muestra tira de
bloque (nombre, cuenta regresiva, barra del plan), marca el **arco objetivo** en los diales de
FC/potencia y avisa dentro/fuera de rango; el cambio de bloque se avisa con un destello
(sin audio: no hay dependencias nuevas).

**CLI.** `hrdash` (y `--3d`) muestra el menu; `--now` lo salta y arranca como hoy (para
scripts, `--simulate` y pruebas); `--duration` y `--ascii` no cambian.

## Fases

0. **Modelo del entrenamiento** (`workouts.py`, puro): `Block`, `Workout`, presets de la tabla,
   validacion y `at(t)`; persistencia en `config.toml`. Sin UI. Harness: suma de cada preset,
   `at(t)` en los bordes, ida y vuelta TOML. **Hecha**, ver "Estado de la fase 0".
1. **Fases del motor y escena `menu` minima**: `Engine.phase`/`begin()`, `--now`, snapshot
   `phase`, escena con titulo y boton Play (raton y Enter) y evento `ui`. Al pulsar Play arranca
   la sesion. Harness: pulsaciones y clics reales en Xvfb (XTest). **Hecha**, ver "Estado de la
   fase 1".
2. **Sensores**: escaneo, lista, asignacion, conexion y valores en vivo, persistencia. **Hecha**,
   ver "Estado de la fase 2".
3. **Sesion**: minutos, presets y editor de intervalos en el menu. **Hecha**, ver "Estado de la
   fase 3".
4. **Plan en ejecucion**: runner, tira de bloque en el HUD, arco objetivo en los diales,
   avisos; columna `bloque` en el CSV.
5. **Export y pulido**: un `lap` por bloque en el FIT (`validate_fit` sigue pasando), resumen
   final, pausa, y confirmar con GPU real.

Cada fase se verifica con capturas offscreen, el smoke del repo, y el menu real bajo Xvfb con
teclado y raton inyectados.

## Riesgos

- **Escaneo y conexion a la vez**: en macOS funciona; en BlueZ puede dar "operation already in
  progress": hay que serializar escaneos y pausar el reintento de la fuente mientras se escanea.
- **Hit-test con HiDPI**: el cursor de GLFW va en puntos y el framebuffer en pixeles (Retina x2):
  normalizar por el tamano de **ventana**, no del framebuffer.
- **Foco en macOS**: GLFW no entrega botones a una ventana sin foco (ya documentado en la
  TUI): el menu debe mostrarlo y aceptar teclado.
- **Sin entrada de texto**: todo se resuelve con presets, listas y +/-.
- **FC en esfuerzos cortos**: ver nota de Tabata/30-30 (objetivo por potencia).
- **Sin rodillo de potencia**: los objetivos por %FTP no se pueden evaluar; el plan degrada a
  objetivos por FC y, si tampoco, a solo cronometro.

## Decisiones por confirmar (valor por defecto entre parentesis)

1. Play y Grabar: (un solo boton **Play = inicia y graba**; casilla "Grabar" activa por defecto,
   desmarcarla permite rodar sin exportar).
2. Tipo de objetivo de los bloques: (por **zona/banda** del repo, con %FTP y %FCmax mostrados;
   FC si no hay potencia).
3. Al cumplirse el plan: (**avisa y sigue libre**; no corta la sesion).
4. Control del rodillo (ERG por FTMS): (**fuera de alcance**: solo lectura, como hoy).
5. `hrdash` sin opciones: (**siempre menu**; `--now` para saltarlo).

## Fuentes

- Noruego 4x4: [BodySpec](https://www.bodyspec.com/blog/post/norwegian_44_hiit_to_boost_vomax),
  [GetFitCraft](https://getfitcraft.com/science/norwegian-4x4-protocol).
- Intervalos de ciclismo (umbral, sweet spot, VO2max):
  [RunBikeCalc](https://runbikecalc.com/blog/cycling-interval-training-complete-guide-2026),
  [Roadman: sweet spot](https://roadmancycling.com/blog/sweet-spot-training-cycling-complete-guide),
  [Roadman: umbral](https://roadmancycling.com/blog/cycling-threshold-intervals-guide).
- Zonas de potencia: [TrainerRoad](https://www.trainerroad.com/blog/cycling-power-zones-training-zones-explained/),
  [Garmin](https://www.garmin.com/en-GB/blog/cycling-training-zones-guide/).
- Calentamiento, enfriamiento y 80/20: [join.cc](https://join.cc/cycling-tips/polarized-training-cycling),
  [Carmichael 55 min](https://trainright.com/?p=24030).

## Estado de la fase 0 (hecha)

`src/hrdash/workouts.py`, sin dependencias de UI ni BLE; `config.py` guarda y lee el plan.

- **Modelo**: `Block(name, kind, secs, ftp, hr)` con objetivo en %FTP y/o %FCmax (alto `None` =
  sin tope) y `Repeat(count, parts, between)`, anidable hasta 3 niveles; la pausa `between` va
  ENTRE repeticiones, no tras la ultima (asi el noruego da 4 esfuerzos y 3 pausas = 40'). Los
  presets de la tabla salen de ahi, mas `free(minutos)` y `custom(calent, reps, trabajo s,
  pausa s, enfriar, ...)` para el editor de la fase 3.
- **`Workout.at(t)`** devuelve `Position` (bloque, transcurrido, restante, restante total,
  siguiente, progreso, `done`). 6 us por consulta con 1.000 bloques.
- **`resolve_target(block, ftp, hrmax, has_power)`**: potencia si hay y el bloque la define; si
  no FC; si tampoco, `None` (solo cronometro). `Target.status(valor)` da `low`/`ok`/`high`.
  Los intervalos de 60 s o menos (30/30, Tabata) no llevan objetivo de FC.
- **Validacion** (`validate`, nunca levanta): nombre, tipo, duracion entera 1..6 h, rangos,
  repeticiones 1..200, anidado <= 3, <= 2000 bloques expandidos (antes de expandir: un
  `Repeat(10**9)` no cuelga nada).
- **Persistencia**: `to_dict`/`from_dict` (la misma forma sirve para mandar el plan a la
  ventana) y `to_toml_inline`: una linea `workout = {...}` en `config.toml`, solo si hay plan.
  `load_config` ignora un plan roto en vez de fallar. Rangos abiertos como `[121]`, igual que
  `zone_pcts`.
- **Harness** (fuera del repo, `workouts_check.py`): cada preset suma lo de la tabla y valida;
  estructura (4+3 del noruego, 12+13 del 30/30, 8 del Tabata, repeticion anidada 1/6 y 6/6);
  `at(t)` segundo a segundo en cada preset (bloque, transcurrido, restante, progreso, orden) y en
  los bordes de cada bloque, mas negativo, `None`, fracciones y valores enormes; 18 planes
  invalidos distintos detectados sin excepcion ni cuelgue; builders; `resolve_target` con y sin
  potencia y sin tope; ida y vuelta JSON y TOML (con comillas, barras y `n`); `config.toml` real
  (guarda, relee, sin plan no escribe la clave, plan roto se ignora). 0 fallas.

## Estado de la fase 1 (hecha)

- **Motor** (`engine.py`): `Engine(menu=True)` arranca en `phase = "menu"`: las fuentes conectan
  y se leen en vivo (`menu_info`), pero `poll()` no anade muestras. `begin()` pasa a `running`:
  `Session`/`LapTracker` nuevos, el simulador reinicia su reloj (`SimSensor.reset_clock`) y la
  distancia del rodillo, que corre desde que conecta, se resta (`_dist0`; tambien en `x`).
  `handle_ui({"act": "play"})` solo actua en fase menu; `x` no hace nada en el menu.
- **Protocolo** (`proto.py`): claves nuevas `phase` y, solo en fase menu, `menu` (opcional: el
  hijo ya tolera claves desconocidas, `PROTOCOL` no sube). El hijo contesta `{"ui": {"act": id}}`.
- **Escena** (`menuview.py`): `menu_widgets(menu, aspecto)` es una funcion PURA que usan igual el
  dibujo y el hit-test, asi la zona de clic no puede separarse del boton. Se dibuja en capas
  `hud_*` (camara ortografica de la segunda pasada), con ajuste de texto por ancho real de trazo
  (`_clip`). Muestra el estado de FC y rodillo (conectado/buscando/error y lectura en vivo) y el
  boton INICIAR con hover.
- **Ventana** (`glview.py`): `phase` compartida con los callbacks. En fase menu NINGUNA tecla de
  vista actua (si no, `g` o las flechas se saltarian el Play): Enter/Espacio/clic = Play,
  `q`/Esc = salir sin grabar. Al pasar a `running` las vistas se resincronizan con las del motor.
- **CLI**: `hrdash`/`--3d` abren el menu; `--now` lo salta. En el menu el bucle sondea a 10 Hz
  (un clic no espera 1 s), manda snapshot a ~2 Hz y uno inmediato tras cada evento. Salir del
  menu sin Play no exporta y devuelve 0 (2 si la ventana nunca abrio).
- **Error previo corregido**: `winkey()` usaba `win.value`, pero en glfw-python 2.11 (Linux) la
  ventana es `LP__GLFWwindow`, sin `.value`: cada callback recibia un objeto distinto y `id(win)`
  nunca coincidia, asi que `slot_under()` devolvia `None` (hover del menu, y tambien rueda, orbita
  con arrastre y seguimiento de foco de la pista). Ahora vale la direccion del puntero
  (`ctypes.cast(win, c_void_p).value`). Verificado con rueda y arrastre reales.
- **Harness** (fuera del repo; XTest sobre Xvfb): teclas de vista (`g`, `->`, `p`, `r`, `3`, `h`,
  `w`, `<-`) en el menu no cambian nada y Enter arranca; clic con hover (brillo del boton 108 ->
  134); Espacio; `q` y Esc salen sin grabar ni crear carpeta de export; `--now` va directo a
  rodar; el CSV de una sesion tras 10 s de menu empieza en 1 m y solo tiene filas posteriores a
  Play (8 de ~18 s de ventana abierta); Ctrl+C en el menu y rodando; sin display (`--3d`: exit 2,
  sin exportar); `workouts_check`, auditoria de dials, escenas y smoke FIT sin cambios.
- Pendiente de confirmar en el Mac: foco y clic reales (GLFW no entrega botones a una ventana sin
  foco) y el tamano del boton con HiDPI.

## Estado de la fase 2 (hecha)

Menu con lista de dispositivos, asignacion de banda y rodillo, valores en vivo y persistencia.

- **Escaneo** (`ble.py`): `scan_devices(timeout, fresh)` devuelve `Found(name, address, rssi, kind,
  caps)` clasificados con `classify`. Usa `return_adv=True`: **bleak 3.x ya no tiene
  `BLEDevice.metadata`**, asi que el codigo anterior solo clasificaba por nombre y nunca por
  servicios (180D/1818/1826); ahora tambien por servicios anunciados, y se ve la senal. Los escaneos
  se serializan con un candado por bucle (BlueZ no admite dos a la vez: compiten las dos fuentes y
  el menu) y un escaneo de menos de 5 s se reutiliza, salvo `fresh=True` (boton Buscar); un error no
  se cachea. `scan_all` se conserva encima (lo usan las fuentes y `--scan`, que ahora imprime la
  senal y da un motivo en vez de un traceback sin Bluetooth). `describe_scan_error` /
  `friendly_error` traducen los fallos a un texto corto ("BLUETOOTH NO DISPONIBLE", "APAGADO",
  "SIN PERMISO...").
- **Motor** (`engine.py`): cada fuente corre en su propia tarea con su propio evento de parada
  (`_runs`), asi se cambia una sin tocar la otra y se cancela la sustituida. Con el simulador
  compartido por las dos ranuras corre UNA vez (antes dos tareas sobre el mismo objeto: distancia
  x1,66 en el simulador). `request_scan` (no se encolan), `assign_device`, `assign_sim`,
  `remove_trainer`, `play_state` (INICIAR exige una FC en vivo: sin ella no se anota nada) y
  `handle_ui` async. `scanner` y `factory` son inyectables. `stop()` conserva las ranuras (export y
  persistencia las leen). El escaneo procesa la respuesta dentro del `try`: antes, una respuesta
  inesperada dejaba el menu en "BUSCANDO..." para siempre.
- **Menu** (`menuview.py`): lienzo fijo de 1,78 x 1 escalado a la ventana; lista (fila SIMULADOR +
  hasta 7 dispositivos con tipo, servicios, barras de senal y "EN USO"), dos tarjetas con estado y
  lectura en vivo, BUSCAR, QUITAR (rodillo) e INICIAR. Interaccion por FOCO: flechas/Tab, Enter o
  Espacio activan, `B` busca, el raton mueve el foco y el clic activa; el foco inicial es INICIAR
  (si esta apagado, el primer widget habilitado). Un widget deshabilitado no recibe foco ni clic.
  Cuatro capas segun lo que cambia (`hud_static`, `hud_list`, `hud_values`, `hud_frame`); el texto
  del menu es plano (`draw_text(depth=0)`), 8 veces mas barato que en cajas. La fuente gana `+ _ ' ?
  ! = # & * < > [ ]` (antes "Rhythm+" se dibujaba sin el signo); los nombres se normalizan
  (`font_text`: sin acentos, `?` para lo que la fuente no tiene) y se recortan al ancho real.
- **Ventana** (`glview.py`): teclas `up/down/tab/b`; fuera del menu esas teclas ya no se
  reenvian a la TUI (con `simulate_key` un Tab o un Enter movian el foco de la consola; ya pasaba
  con `enter/space/escape` desde la fase 1).
- **Persistencia** (`cli.py`, `config.py`): `last_device`/`last_trainer` guardan la direccion si
  se eligio por direccion (distingue dos dispositivos con el mismo nombre) y si no el nombre; nueva
  clave `use_trainer` (`false` solo cuando el usuario pulso QUITAR; elegir un rodillo la
  restablece). El simulador nunca se guarda. **Error previo corregido**: `cmd_3d` guardaba DESPUES de
  `eng.stop()`, cuando las fuentes ya estaban desconectadas, asi que en modo 3D el ultimo sensor no
  se guardaba nunca; ahora se guarda en el `finally`, antes de parar.
- **`--simulate`**: abre con el simulador en las dos ranuras (INICIAR habilitado ya) y el escaneo
  devuelve una banda y un rodillo de demostracion que se pueden asignar (`sim_scan`,
  `sim_factory`). Sin `--simulate`, la fila SIMULADOR permite probar sin hardware.
- **Harnesses** (fuera del repo): `menu_audit.py` (256 widgets en 8 formas de ventana y 5 estados:
  dentro de pantalla, sin solapes, clic en el centro de cada uno, foco que recorre todo y vuelve,
  nunca cae en un deshabilitado, fuente sin caracteres fuera de rango); `engine_check.py` (asignar,
  reasignar, descartar basura, cancelacion de la fuente sustituida, simulador una vez, `begin`
  cancela el escaneo, `stop` sin tareas colgadas, errores y respuestas raras del escaner, y
  `scan_devices` con un `BleakScanner` falso: 5 llamadas simultaneas -> 1 real, TTL, `fresh`, error
  no cacheado); `phase2_run.py` + `phase2_drive.sh` (motor y ventana reales con un escaner y unas
  fuentes falsas, y teclas y raton fisicos por XTest: INICIAR apagado sin FC y el clic no hace
  nada, `B` dos veces = un escaneo, asignar por teclado y por clic, cambiar de banda, quitar el
  rodillo, iniciar; el informe comprueba 2 escaneos, 1 simultaneo, todas las fuentes sustituidas
  canceladas, `last_device`/`use_trainer` guardados y un CSV solo posterior a INICIAR); y los
  escenarios de la fase 1 (teclas, clic, Espacio, `q`, Esc, `--now`), sin cambios.
- No hecho / limites: no se muestran dispositivos que no anuncien ni un nombre reconocible ni un
  servicio conocido (no hay "mostrar todos"); la lista no desplaza (7 dispositivos); sin Bluetooth
  real no se pudo probar la conexion de verdad (solo escaneo y fuentes falsas); y en macOS, el clic
  con la ventana recien abierta y el tamano con HiDPI siguen por confirmar.

## Estado de la fase 3 (hecha)

El menu elige la sesion: minutos, un preset o intervalos propios. Esta fase **elige, valida y guarda**
el plan; que la sesion lo siga en vivo (tira de bloque, arco objetivo, avisos) es la fase 4.

- **Modelo** (`workouts.py`): `SessionSpec(kind, minutes, preset, custom)` y `CustomSpec(warm, reps,
  work_s, rest_s, cool, work, rest)`, pequenas, inmutables y hashables. `build()` da el `Workout` de
  siempre (o `None` si es libre y sin limite: solo cronometro, que es lo que hacia la app antes y es
  el valor por defecto). `apply_act(spec, accion)` es una funcion pura que aplica una accion del menu
  (`kind:...`, `min:set:N`, `min1:+-`, `min5:+-`, `cu:<campo>:+-`) y nunca devuelve un estado fuera
  de rango: las duraciones salen de una escalera (5 s, 10 s ... 20 min; `+`/`-` salta al siguiente
  valor aunque el actual no este en ella), los niveles dan la vuelta, y un plan que pasaria de 6 h
  se rechaza. Ayudas para dibujar: `intensity`/`band` (colorean el perfil por %FTP; los bloques
  solo por FC se convierten con una tabla aproximada), `profile` y `describe` (lineas cortas en
  ASCII; las largas usan nombres de nivel en vez de porcentajes).
- **Config**: `session = {...}` (una linea, solo si no es la de por defecto) **reemplaza al campo
  `workout` de la fase 0**, que nada usaba; una clave `workout` antigua se ignora y desaparece al
  guardar, y una sesion rota o mal tipada vuelve al valor por defecto sin impedir el arranque.
- **Motor** (`engine.py`): `spec`, `page` y `workout`. `handle_ui` entiende `page:...` ademas de las
  acciones de sesion; `begin()` construye el plan elegido; `menu_info` manda `page` y `session`
  (spec, nombre, duracion, lineas y perfil). `persist_choices` guarda la sesion junto al resto.
  Al iniciar, la terminal imprime `plan: ...` en el resumen.
- **Menu** (`menuview.py`): dos paginas con pestanas (`1` SENSORES, `2` SESION). SESION: lista de
  tipos (libre, los 8 presets, personalizada) y a la derecha, segun el tipo, minutos (dos steppers
  de +-1 y +-5, atajos 15/30/45/60/90 y SIN LIMITE), la descripcion del preset, o el editor
  (calentar, series, fuerte, nivel, suave, nivel, enfriar), mas la duracion total y el perfil de
  bloques (barra por bloque: ancho = duracion, alto y color = intensidad). Un STEPPER se ajusta con
  Izquierda/Derecha estando enfocado o con el clic a cada lado del valor (`hit_act`); Enter suma.
  Sin FC, el foco por defecto es lo primero util de la pagina (BUSCAR, o el primer tipo) y no una
  pestana.
- **Ventana** (`glview.py`): teclas `1`, `2`, `left` y `right` (estas dos solo en el menu); los
  clics mandan la accion completa, con el lado en los steppers.
- **Harnesses** (fuera del repo): `spec_check.py` (30.000 acciones aleatorias, un 15 % basura, sin un
  estado invalido ni una excepcion; escaleras con valores fuera de ella, tope de 6 h, 25 formas de
  `spec_from_dict` rechazadas con solo `ValueError`, ida y vuelta TOML de 300 specs y del
  `config.toml` real incluido el caso de una clave `workout` antigua); `menu_audit2.py` (3.416
  widgets en 8 formas de ventana, 2 paginas y 13 sesiones: dentro de pantalla, sin solapes, clic a
  cada lado de cada stepper, foco que recorre todo, y que ningun texto se corte ni pise las flechas,
  incluidos los 8 presets y los valores extremos); `engine_check.py` y `menu_audit.py` actualizados;
  y el recorrido real con XTest (`phase3_drive.sh`): pagina 2, preset por clic, personalizada con
  Izquierda/Derecha y clics a cada lado, libre con atajo y steppers, 30/30; la configuracion
  guardada coincide campo a campo con lo hecho, reaparece al reabrir y el resumen imprime el plan.
  Los escenarios de las fases 1 y 2 se repiten sin cambios (esa repeticion encontro el foco por
  defecto detras de las pestanas).
- No hecho / limites: el plan se sigue en vivo desde la fase 4 y se exporta en la 5; los presets no son
  editables (se copian a "personalizada" a mano); el editor no hace series con varios niveles
  (calentar, N x fuerte/suave, enfriar); las pausas entre series largas del 30/30 no se pueden
  construir en el editor; y en macOS siguen por confirmar el clic con la ventana recien abierta y el
  tamano con HiDPI.


## Estado de la fase 4 (hecha)

El plan elegido se sigue durante la sesion. El reloj del plan es el de `Session` (segundos desde
`begin()`, o desde el arranque con `--now`), asi que `x` (reiniciar) tambien lo reinicia.

- **Motor** (`engine.py`): `plan_info()` da lo que se dibuja (bloque actual con su indice, repeticion,
  cuenta atras, siguiente bloque, avance, perfil, objetivo resuelto y su estado `low`/`ok`/`high`) y
  viaja en la clave opcional `workout` del snapshot (no sube `PROTOCOL`; sin plan la clave no esta).
  `block_label()` ("Esfuerzo 2/4", o "libre" cuando el plan termino) alimenta la columna `bloque`.
  Objetivo por potencia si hay lectura del rodillo y el bloque la define; si no, por FC.
  Terminado el plan, `done` + `over` (segundos de mas): se avisa y la sesion sigue libre.
- **Tira del plan** (`cluster.plan_hud`, capa `hud_plan`, a 1 Hz): nombre y repeticion, siguiente
  bloque, cuenta atras, rango objetivo con su estado (EN RANGO / SUBE / BAJA) y una barra con un tramo
  por bloque (color = banda) y el marcador del momento. Los primeros 4 s de cada bloque el fondo
  alterna cada segundo (destello; sin audio). En la pista va bajo la tira digital; en el tablero va
  arriba y la camara se aleja un poco para dejarle sitio (sin plan, el encuadre es el de siempre).
- **Arco objetivo** (`cluster.dial_target`): sobre el bisel del dial de FC o de potencia, del color
  del estado, en tablero y en el HUD de la pista.
- **CSV**: columna `bloque` solo si hubo plan; sin plan el CSV es el de siempre. La terminal imprime
  `[mm:ss] bloque: ...` en cada cambio.
- `--now` aplica la sesion guardada; `--duration` (headless) no.
- Verificacion: `plan_check.py` (posicion en los bordes, estados, fin del plan, CSV con y sin
  columna, sesion libre con minutos), renders offscreen del tablero y la pista en 8 casos, y una
  sesion real bajo Xvfb con `--simulate --now` (tira y arco en la ventana, columna en el CSV).
  Auditorias de menu, dials y el smoke del FIT sin cambios.
- No hecho (fase 5): un lap por bloque en el FIT, resumen por bloque, pausa y la prueba con GPU real.

## Estado de la fase 5 (hecha, salvo la prueba con GPU real)

- **Un lap por bloque en el FIT** (`export.export_fit`): `Session.blocks()` agrupa las muestras
  consecutivas con el mismo `bloque` (el tramo final "libre" cuenta) y cada tramo es un `lap` con su
  FC, potencia, cadencia, velocidad, distancia, kcal y tiempo por zona; `num_laps` de la sesion es el
  numero de tramos. Sin plan sigue habiendo un solo lap. `validate_fit` ahora tambien compara el
  numero de laps con el esperado.
- **Resumen final por bloque** (`cli.print_summary`): una linea por bloque (tiempo, FC media y maxima,
  W medios), tope de 40 lineas.
- **Pausa**: `espacio` en la ventana (solo rodando; en el menu sigue siendo "activar"). Congela el
  reloj de la sesion y del plan (`Session.pause/resume`, `elapsed_s` no cuenta la pausa), no anota
  muestras y descarta la distancia que el rodillo siga contando (`_pause_dist`). Cartel PAUSA en el
  tablero y la pista (`cluster.pause_hud`); las vistas FC y watts no tienen capa HUD y no lo muestran.
  El FIT lleva `total_timer_time` sin la pausa y el resumen imprime el tiempo en pausa. Salir en
  pausa cierra la pausa antes de grabar.
- Verificacion: `plan_check.py` ampliado (tramos, FIT con 4 laps decodificado, suma de laps, sin plan 1
  lap, pausa: reloj congelado, sin muestras, distancia continua, snapshot) y una sesion real bajo Xvfb
  con plan corto y pausa: 4 cambios de bloque, tabla del resumen, CSV por bloque y FIT round-trip OK.
- **Pendiente en tu Mac** (no se puede probar aqui): clic al abrir la ventana, tamano con Retina, BLE
  real, fluidez con GPU real (vista mixta), pausa con espacio y la tira del plan con tu banda y rodillo.

## Fase 6 (analisis): dos monitores

Factible. Lo que ya hay y lo que falta:

- `glview.open_windows` ya tiene un modo "una ventana por monitor" (`multi_mode`: 2+ vistas y al menos
  tantos monitores como vistas): crea una ventana por vista, cada una con su contexto moderngl y
  `swap_interval(0)`. Nunca se ha probado con dos pantallas reales; si algo falla vuelve al split en una
  sola ventana. En este Linux (Mesa) se comprobo que dos ventanas con dos contextos moderngl se crean
  y dibujan sin problema, asi que la preocupacion del comentario del codigo (que el segundo contexto
  no se detecta) no se reproduce aqui; en macOS hay que probarlo.
- Falta, para "pista arriba y tacometros abajo": (1) **asignar vista a monitor por posicion**, no por
  el orden de `glfw.get_monitors()`: ordenar por `get_monitor_pos` de arriba a abajo (o `--monitor-view
  pista=1 gauge=2`) y abrir cada ventana en su monitor; (2) que cada ventana dibuje el HUD que le toca
  (la tira del plan y el cartel PAUSA deberian ir en la de los tacometros o en las dos); (3) un solo
  vsync para las dos ventanas (hoy en modo multiple se apaga y se limita a ~60 fps por reloj; hay que
  medir tirones); (4) el menu previo a Play seguiria en el monitor principal y al iniciar se abre la
  segunda ventana; (5) cerrar una ventana o desconectar un monitor debe volver al split sin matar el hijo.
- Verificacion posible aqui: Xvfb con dos pantallas (Xinerama) para la logica de asignacion y que ambas
  ventanas dibujen; la fluidez y el comportamiento en macOS (Spaces, ventanas a pantalla completa en el
  segundo monitor) solo se confirman en tu Mac.
- Riesgo principal: dos contextos GL por proceso en macOS (CoreVideo/vsync por pantalla). Alternativa
  si falla: una sola ventana sin bordes que ocupe los dos monitores apilados (solo funciona si macOS
  tiene desactivado "las pantallas tienen Spaces independientes").

## Estado de la fase 6 (hecha en Linux/Xvfb; falta probarla con dos pantallas reales)

`hrdash --dual --3d-view pista --3d-view gauge` (o en cualquier orden): una ventana por vista, cada una
a pantalla completa en su monitor, con la pista en el de arriba. Sin `--dual` nada cambia.

- **Asignacion por posicion** (`glview._monitor_rects`, `dual_assign`): los monitores se ordenan por su
  posicion en el escritorio (de arriba a abajo y de izquierda a derecha), no por el orden de GLFW; las
  vistas siguen `VIEW_MONITOR_ORDER` (pista, tacometros, FC, watts), asi que la pista cae en el monitor
  de arriba pidas el orden que pidas. Hace falta un monitor por vista; si no, ventana partida como siempre
  y la terminal avisa ("doble monitor pedido pero hay N pantalla(s) para M vistas").
- **El menu sigue en una sola ventana** (la primaria, en el monitor principal). Al pulsar Play la primaria
  se muda a su monitor (`set_window_monitor`) y se abre la segunda ventana, con contexto compartido
  (`share`) y su propio contexto moderngl, programa y VAOs.
- **Vsync**: solo la primaria espera al refresco; las demas van con `swap_interval(0)` y se intercambian
  antes. El tope de ~16 ms del bucle sigue valiendo.
- **`AUTO_ICONIFY` apagado** para todas las ventanas: con pantalla completa en dos monitores, un clic en
  una hacia que la otra se minimizara.
- Cada ventana dibuja su HUD: la tira del plan, el arco objetivo y el cartel PAUSA salen en las dos.
  Teclas, rueda y arrastre actuan sobre la ventana que tiene el foco.
- Prueba sin dos pantallas: `HRDASH_FAKE_MONITORS="0,0,1920,540;0,540,1920,540"` simula monitores
  apilados dentro de un Xvfb (ventanas normales en cada rectangulo, sin pantalla completa). Verificado:
  `--now`, menu -> Play -> dos ventanas, pausa en ambas, un solo monitor -> ventana partida con el
  aviso, y sin `--dual` como antes. No cubre pantalla completa real ni vsync por pantalla.
- Sin hot-plug: los monitores se leen al abrir.
- **Pendiente en tu Mac**: pantalla completa en el segundo monitor (Spaces), fluidez con dos contextos y
  que un clic en una ventana no minimice la otra.
