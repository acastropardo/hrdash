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
2. **Sensores**: escaneo, lista, asignacion, conexion y valores en vivo, persistencia.
3. **Sesion**: minutos, presets y editor de intervalos en el menu.
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

