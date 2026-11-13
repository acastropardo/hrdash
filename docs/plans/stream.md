# hrdash --stream: el motor para clientes graficos externos

Objetivo: un solo motor (BLE, sesion, FIT, menu, plan) y varios clientes graficos que hablan el
mismo protocolo: la ventana OpenGL (`hrdash`), la consola (`--ascii`) y un cliente externo como el
de Godot (`--stream`). Cada arreglo de BLE, FIT o plan se hace una vez, aqui.

## Que cambia

- `src/hrdash/stream.py`: `StreamHost`, con la interfaz de `GLHost` (`start`, `alive`, `push`,
  `drain`, `stop`, `error`) sobre un servidor WebSocket en 127.0.0.1. Corre en un hilo propio con su
  bucle de asyncio; el motor no espera a la red. Aguanta sin cliente, con varios y con clientes que
  se caen; el que conecta recibe al instante el ultimo snapshot (el menu aparece sin esperar).
- `cli.py`: `--stream` (en el grupo exclusivo de modos; `--simulate` y `--now` siguen fuera) y
  `--port` (default 8765). Con `--stream` no se abre ventana: el motor espera cliente. `--dual` no
  aplica. `cmd_3d` no cambia de flujo: solo cierra el host DESPUES de exportar si el host lo pide
  (`keeps_open`), para que el cliente reciba el `summary` por la conexion abierta, como hoy se
  imprime en consola.
- `proto.py`: `PROTOCOL = 2`, documentando `hello` y `summary`. Los snapshots no cambian; el hijo
  OpenGL no recibe esos mensajes.
- `pyproject.toml`: extra `stream = ["websockets>=12"]`; el nucleo no lo exige.

## Contrato (aditivo; un cambio incompatible sube `PROTOCOL`)

Motor -> cliente (un JSON por mensaje):
- `{"hello": {"protocol": N, "views": [...], "app": "hrdash"}}` al conectar.
- snapshot: el de `proto.build_snapshot` (con `phase`, `menu`, `workout`, `paused` opcionales) mas
  `version`. 1 Hz rodando, 2 Hz en el menu y uno inmediato tras cada accion `ui`.
- `{"summary": {...}}` al salir, tras exportar.

Cliente -> motor:
- `{"hello": {"client": "godot", "views": [...]}}`: cuenta la ventana como abierta.
- `{"key": "q|x|s|space"}`, `{"views": [...]}`, `{"zoom": ...}`.
- `{"ui": {"act": "<id>"}}`: las acciones del menu (`Engine.handle_ui`: `play`, `scan`, `dev:<id>`,
  `page:sesion`, `kind:preset:noruego`, `min:set:30`, `cu:warm:+`...).
  Modo de fuentes: `sim` pasa al simulador (escaneo y elecciones de demostracion) y `real` vuelve a los sensores
  reales (suelta el simulador, recupera la ultima banda y rodillo guardados y refresca el escaneo). El menu lleva
  `can_real: true` cuando el motor entiende `real`; `--simulate` solo decide en que modo arranca (`Engine(demo=...)`).

## Carrera (persecucion contra fantasma)

- Acciones `ui` nuevas: `kind:race`, `race:dist:+|-` (1-4 km), `race:ghost:pace|best` y `race:target:+|-` (pasos de 5 s). La
  sesion elegida (`menu.session`) lleva `kind: "race"` y la clave `race` con distancia, fantasma y tiempo objetivo.
- Al pulsar Play con `kind == "race"` el motor crea `race.PursuitRace` (cuenta atras de 3 s, salida parada) y, mientras
  corre, sondea a 10 Hz: cada snapshot trae la clave aditiva `race` = `{event, state, paused, distance_m, t, s: [jugador,
  fantasma], v: [km/h jugador, km/h fantasma], gap_s, gap_m, lap, laps, splits, countdown, wheel_kmh, note?, final_s?,
  ghost_s?, pb?}`. `speed`, `dist_km` y la posicion en la pista de la sesion son los virtuales (de la potencia); la velocidad
  de la rueda va en `race.wheel_kmh`. El muestreo de la sesion sigue a 1 Hz (CSV/FIT con `Salida`, `Vuelta n/N`, `libre`).
- `space` pausa la carrera y la sesion; `x` reinicia la carrera (cuenta atras otra vez). Al terminar se guarda la marca en
  `races.json` (junto a `config.toml`) y `race.pb` dice si fue record personal. `PROTOCOL` no cambia (clave aditiva).

## Scratch contra el pelotón de IA

- Acciones `ui`: `kind:scratch`, `scratch:laps:+|-` (8/12/16/20 vueltas de 250 m) y `scratch:level:+|-|easy|even|hard`. La sesion
  (`menu.session`) trae `scratch: {laps, level, seed}` y `lines` con el FTP de los rivales.
- Al iniciar, `peloton.ScratchRace`: el jugador y 3 rivales de IA (FTP = tu FTP x factor del nivel, semilla fija) con el mismo modelo fisico,
  rebufo (hasta 33 % de ahorro de aire a rueda, nulo a 10 m), reserva W' de 15 kJ, tacticas (rodar a rueda, ataques, esprint final)
  y cambio de linea para pasar. Clave `race` del snapshot con `event: "scratch"`: `{state, paused, distance_m, t, lap, laps, pos, n,
  countdown, riders: [{id, name, color, player, s, q, v, pos, draft, gap_m}], alerts: [{t, text}], results? (al terminar:
  [{id, name, color, player, time_s, pos, gap_s}]), wheel_kmh}`. Misma pausa, `x` y etiquetas `Salida` / `Vuelta n/N` / `libre`.

## Verificacion

El harness vive fuera de este repo (convencion de AGENTS.md); esta en `hrdash-godot/harness/
test_stream.py` y lanza `hrdash --simulate --stream` contra un cliente WebSocket falso:

- `hello` con protocolo 2; snapshot de fase menu valido (`proto.validate`) con bloque `menu`.
- Un segundo cliente recibe `hello` y el ultimo snapshot al conectar; si se cae, el motor sigue.
- Acciones `ui`: `page:sesion`, `kind:preset:noruego`, `play` -> fase `running` con `workout`.
- `version` creciente, snapshot rodando valido, pausa y reanuda con `space`, cambio de `views`.
- `q` entrega el `summary`, deja el CSV exportado y el motor sale con 0.
- Puerto ocupado: sale con 2 y un mensaje.
- Sin regresion: `--simulate --duration 3` (FIT round-trip) y el camino OpenGL (`cmd_3d` con
  `GLHost` bajo xvfb) se comprobaron igual que antes.

## Riesgos

- `websockets` en un hilo propio dentro de `cmd_3d`: el cierre (`q`) deja el export en el hilo
  principal y el host abierto hasta `send_summary`, con un tope de 2 s si el cliente no lee.
- El protocolo es el punto de ruptura entre repos: cambios solo aditivos y version en el `hello`.


## Prueba de puntos (sobre el pelotón del scratch)

`ScratchSpec.event` ("scratch" | "points"; acto `scratch:event:+|-|scratch|points`; configs antiguas sin la clave = scratch).
En puntos hay un sprint intermedio cada 4 vueltas (5/3/2/1 puntos por orden de cruce) y la meta vale el doble; gana quien suma
mas (empate: quien cruza antes la meta). Atacantes y velocistas disputan los sprints con W'. El snapshot `race` conserva
`event: "scratch"` (el cliente dibuja igual) y anade `mode: "points"`, `pts` por corredor, `sprint_next` (vuelta completada del
proximo sprint), `sprint_to_m`, `sprint_x2` y en `results` `pts`/`sprint_pts`. Aditivo: PROTOCOL sin cambios.
