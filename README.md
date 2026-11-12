# HRDash

Dashboard de **frecuencia cardíaca + telemetría de rodillo** a pantalla completa en la terminal.
Lee un sensor BLE estándar (perfil Heart Rate `0x180D`, ej. **Scosche Rhythm/Rhythm+**,
Polar H9/H10, Garmin HRM, Coospo…) y opcionalmente un rodillo (Tacx u otro) vía
**Cycling Power `0x1818`/`0x2A63`**, **FTMS Indoor Bike `0x1826`/`0x2ACF`** o
**CSC `0x1816`/`0x2A5B`**, y muestra cada segundo:

- 4 números **gigantes**: FC · WATTS · km/h · rpm, coloreados (zona FC / %FTP)
- **barras de FC 1 muestra/segundo** a todo el ancho, con el color de cada zona
- **barras de watts** por banda de %FTP y líneas de **velocidad y cadencia** (8 niveles)
- FC **max / media / min**, W max/media, **W/kg**, vel/cad, **distancia**
- **vista tablero** (`g`): gauges estilo cuentavueltas dibujados con braille — arco de
  zonas FC/%FTP, aguja con inercia a 10 fps, marcas de media y números gigantes
- **ventana 3D** (`3`): escena OpenGL nativa de la vista activa (barras de FC por zona,
  cajas de potencia, dial con aguja y velódromo con peralte) en un proceso aparte
- **gasto calórico** en vivo (fórmula Keytel) según edad, sexo y peso
- **tiempo y kcal por zona** (Z1–Z5)
- al salir graba la sesión en **FIT compatible con Garmin Connect** (records con FC,
  potencia, cadencia, velocidad y distancia) + CSV espejo

Corre en **macOS** (CoreBluetooth) y **Linux** (BlueZ) con el mismo código.

## Instalación

```bash
cd fc
uv venv --python 3.12 .venv        # o: python3.12 -m venv .venv
uv pip install -p .venv -e .       # o: .venv/bin/pip install -e .
uv pip install -p .venv -e ".[gl]" # extras para la ventana 3D (moderngl, glfw, numpy)
source .venv/bin/activate          # o alias: alias hrdash="$HOME/Documents/fc/.venv/bin/hrdash"
```

## Uso

```bash
hrdash                    # ventana 3D (arranca en los tacometros), sin consola; pregunta edad/sexo/peso
hrdash --ascii            # la consola de siempre (TUI) en la terminal
hrdash --3d               # 3D explicito: si no hay GL o pantalla, falla en vez de caer a --ascii
hrdash --3d-view pista    # 3D con otra vista inicial (hr, watts, gauge, pista); repetible para partir
hrdash --scan             # lista sensores de FC y rodillos detectados y sale
hrdash --probe 12         # vuelca GATT y payloads crudos del dispositivo (diagnóstico)
hrdash --sensor "R+2.0" --trainer "Tacx"   # fuerza nombres o direcciones
hrdash --no-trainer       # solo banda de FC
hrdash --simulate         # demo sin hardware (curva sintética FC+W+vel+cad)
hrdash --age 47 --sex M --weight 72 --ftp 250   # sin preguntas
hrdash --duration 30      # modo headless: 30 s de muestreo + grabación (para scripts)
hrdash --fresh            # vuelve a preguntar aunque haya config guardada
```

Por defecto `hrdash` abre la **ventana 3D sin consola**: el motor muestrea y graba igual,
la terminal solo muestra una linea de estado y el resumen al salir (`q` en la ventana o
Ctrl+C en la terminal graban FIT + CSV). Arranca en los tacometros; dentro de la ventana
`←`/`→` rotan entre vistas (FC → watts → tacometros → pista) y `h`/`w`/`g`/`p` saltan a
una, `3`/`r` abren o cierran el tablero o la pista, `v` cambia la camara, `i` muestra u
oculta los tacometros de la pista, `+`/`-` o la rueda hacen zoom y `x`/`s` reinician o
reconectan. Si el 3D no puede arrancar (sin el extra `gl` o, en Linux, sin pantalla),
`hrdash` cae a la consola con un aviso.

Teclas dentro de la consola (`--ascii`):

| tecla | acción |
|---|---|
| `q` | salir y **grabar FIT + CSV** |
| `x` | reiniciar la sesión (mismos datos) |
| `r` | abre/cierra la **ventana 3D de la pista** (velodromo con tacometros) |
| `s` | reconectar banda y rodillo |
| `c` | cambiar edad / sexo / peso / **FTP** en caliente |
| `h` | vista principal del gráfico: **FC** (default) |
| `w` | vista principal del gráfico: **WATTS** (la tira chica muestra siempre la métrica opuesta) |
| `g` | **vista tablero**: gauges tipo tacómetro (FC grande + watts, vel, cad y panel digital) |
| `p` | **vista pista**: circuito tipo velódromo con la bici en la vuelta en curso |
| `3` | abre/cierra la **ventana OpenGL nativa** con la escena 3D de la vista activa |
| `←` / `→` | navegar entre vistas: **FC → watts → tablero → pista** (la ayuda marca la activa) |
| `[` / `]` | en la vista pista: recorre los largos de vuelta (1, 2, 3, **5**, 10, 20 km) |
| `v` | en la ventana 3D de la pista: cambia la camara (persecucion, TV fija, grua) |
| `i` | en la ventana 3D de la pista: muestra u oculta los tacometros (HUD de carreras) |

### Ventana 3D (`3`)

La TUI y el muestreo siguen intactos; el 3D es una **ventana OpenGL nativa aparte**
(`src/hrdash/glview.py`), porque Terminal.app no soporta ningún protocolo gráfico y por
lo tanto no puede mostrar una imagen dentro del panel. La ventana corre en su propio
proceso (GLFW + moderngl: cámara en perspectiva, z-buffer y lighting de Lambert) y el
dashboard le manda el estado una vez por segundo por `stdin`; ella anima localmente a
~50 fps (aguja con inercia, bici recorriendo la vuelta, cámara orbital).

- `3` es un **toggle global**: la escena que se ve es siempre la de la vista activa, y
  el modo **persiste** al cambiar de vista con `h`/`w`/`g`/`p` o `← →`.
- La ventana abre a **1920×1080 píxeles reales**: GLFW mide en puntos, así que se divide
  por el `content scale` del monitor (960×540 puntos en una Retina ×2) y se elige el
  monitor cuya área útil lo aloja; si ninguno puede, se usa el 16:9 más grande que quepa.
- Escenas: **FC** → barras 3D del historial coloreadas por zona + barra de la FC actual;
  **watts** → cajas 3D por banda de %FTP (altura = kJ); **tablero** → **cluster completo de
  los 4 diales** (ver abajo); **pista** → velódromo con peralte, meta y estela.
- Si la ventana se cierra o falla (falta GPU/dependencias), el dashboard **vuelve solo
  a 2D** y sigue grabando; el aviso aparece en la línea de ayuda.

### Cluster del tablero 3D (`g` + `3`)

La vista tablero reproduce en 3D la misma información que la TUI, con los **cuatro diales**
y todo a pantalla llena (`src/hrdash/cluster.py`):

- **Diales**: FC y potencia (grandes), velocidad y cadencia (medianos). Cada uno con
  **banda de zonas** en la corona, **palitos** tipo tacómetro (los mayores más altos y
  anchos) que se **iluminan hasta el valor actual**, aguja con contrapeso y cubo
  metálico, números de escala y lectura digital en **7 segmentos** en las 6 en punto.
- **Columna LED** vertical de FC en el centro (roja en el tramo final).
- **Tira digital**: tiempo / kcal / distancia arriba; FC med·max·%FCmax·estimado y
  W med·max·FTP·en zona en el centro; zona, muestras, kcal/min, W/kg y edad/peso al pie.
- **Pilotos** de zona FC y de banda de potencia (el activo encendido) y **halo rojo
  pulsante** de alarma cuando la FC pasa del 95 % de la máxima.
- **Self-test** al abrir: las cuatro agujas hacen el barrido 0→máx→0 como un auto real,
  y después la aguja conserva la inercia de la TUI (sube rápido, baja suave).
- Los números se dibujan con cajas (7 segmentos y fuente pixel 5×7): no hay atlas de
  fuente ni dependencias extra de render de texto.

### Vista pista (`p`)

Circuito dibujado en celdas braille: dos rectas y dos semicírculos, con la línea de
meta y marcas de cuarto de vuelta. La posición de la bici es **virtual** — se calcula
con la distancia acumulada de la sesión (`distancia % largo de vuelta`), así que sobre
el rodillo la bici avanza como si rodara en un circuito de ese largo.

- Vueltas de **5 km** por defecto (`lap_m` en la config, `--lap 5`, o `[` / `]` en caliente).
- Tablero de vueltas: vuelta en curso, tiempo, FC media/max, watts medios, kJ, kcal y
  tiempo de cada vuelta cerrada.
- Si el rodillo no manda distancia (FTMS/CSC), la distancia se **estima** integrando la
  velocidad, y el tablero lo indica.
- Las vueltas son solo visualización: el FIT sigue grabando un único lap, como antes.

Datos y salidas:

- Config persistente: `~/.config/hrdash/config.toml` (edad, sexo, peso, FTP, cortes de
  zona, perímetro de rueda, último sensor/rodillo).
- Exportación al salir con `q`: `./exportes/hrdash_<fecha>_<hora>.fit` y `.csv`
  (`--export-dir` para cambiar).
- **Autoguardado**: cada 5 minutos se reescribe `hrdash_<...>_AUTO.fit/.csv` (mismo
  archivo, nunca crece), y si un gráfico lanza una excepción se vuelca
  `hrdash_<...>_RESCATE.*` sin interrumpir el muestreo. Ambos son importables en
  Garmin Connect igual que el final.

## Rodillos / telemetría

- **Watts y cadencia**: Cycling Power (`1818`/`2A63`). La cadencia se deriva del delta
  de revoluciones de biela.
- **Velocidad y distancia**: FTMS Indoor Bike (`1826`/`0x2ACF`) si el rodillo lo expone;
  si no, CSC (`1816`/`0x2A5B`) con el perímetro de rueda `--wheel-mm` (default 2135 mm, 28").
- **Tacx Satori y otros modelos viejos**: transmiten datos "fuera de orden" (el bloque de
  biela llega sin declararlo en flags, el CSC manda el contador antes que el tiempo).
  El parser de HRDash es tolerante a esas variantes; si un rodillo se comporta raro,
  pasá `hrdash --probe 15 --trainer "TACX"` pedaleando y mirá las líneas decodificadas.
- El color de watts usa el **FTP** (default 240 W, editable con `c`): azul <56 %, verde
  <76 %, amarillo <91 %, naranja <106 %, rojo ≥121 %.

## Permisos Bluetooth

**macOS**: la primera conexión el sistema pide permiso de Bluetooth para tu terminal
(Terminal/iTerm/Warp/WezTerm…). Si se denegó: *Ajustes del Sistema → Privacidad y
seguridad → Bluetooth* y activarlo. El **Scosche Rhythm+ solo transmite cuando está
puesto** (suelto sobre la mesa no aparece en el escaneo). No hace falta emparejarlo:
HRDash se conecta directo como central BLE.

**Linux**: requiere BlueZ activo (`sudo systemctl enable --now bluetooth`).
Si el escaneo falla por permisos D-Bus: `sudo usermod -aG bluetooth $USER`
(reabrir sesión). `bleak` usa el adapter por defecto; si hay varios, `BLUETOOTH_ADAPTER=hci1`.

## Subir el FIT a Garmin Connect

Arrastrá el `.fit` en <https://connect.garmin.com/activities/upload>. Se registra como
actividad con FC, potencia, cadencia, velocidad y distancia por segundo, media/máx,
calorías y tiempo en zona. HRDash hace un *round-trip* (decodifica el FIT recién
escrito con el SDK oficial y compara records/media/max) y muestra `round-trip OK` al salir.

## Cálculo de calorías (Keytel)

- Hombre: `(-55.0969 + 0.6309·FC + 0.1988·peso + 0.2017·edad) / 4.444` kcal/min
- Mujer:  `(-20.4022 + 0.4472·FC + 0.1213·peso + 0.1574·edad) / 4.444` kcal/min
- FCmáx estimada por Tanaka: `208 − 0.7·edad` (fijable con `hrmax` en la config).

## Zonas de FC

| zona | color | %FCmáx |
|---|---|---|
| Z1 Recuperación | azul | 50–60 % |
| Z2 Aeróbico | verde | 60–70 % |
| Z3 Tempo | amarillo | 70–80 % |
| Z4 Umbral | naranja | 80–90 % |
| Z5 Máximo | rojo | 90+ % |

Límites editables en `~/.config/hrdash/config.toml` → `zone_pcts`.
