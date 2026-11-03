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
source .venv/bin/activate          # o alias: alias hrdash="$HOME/Documents/fc/.venv/bin/hrdash"
```

## Uso

```bash
hrdash                    # pregunta edad [47], sexo [M], peso [70]; banda + rodillo auto
hrdash --scan             # lista sensores de FC y rodillos detectados y sale
hrdash --probe 12         # vuelca GATT y payloads crudos del dispositivo (diagnóstico)
hrdash --sensor "R+2.0" --trainer "Tacx"   # fuerza nombres o direcciones
hrdash --no-trainer       # solo banda de FC
hrdash --simulate         # demo sin hardware (curva sintética FC+W+vel+cad)
hrdash --age 47 --sex M --weight 72 --ftp 250   # sin preguntas
hrdash --duration 30      # modo headless: 30 s de muestreo + grabación (para scripts)
hrdash --fresh            # vuelve a preguntar aunque haya config guardada
```

Teclas dentro del dashboard:

| tecla | acción |
|---|---|
| `q` | salir y **grabar FIT + CSV** |
| `r` | reiniciar la sesión (mismos datos) |
| `s` | reconectar banda y rodillo |
| `c` | cambiar edad / sexo / peso / **FTP** en caliente |

Datos y salidas:

- Config persistente: `~/.config/hrdash/config.toml` (edad, sexo, peso, FTP, cortes de
  zona, perímetro de rueda, último sensor/rodillo).
- Exportación: `./exportes/hrdash_<fecha>_<hora>.fit` y `.csv` (`--export-dir` para cambiar).

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
