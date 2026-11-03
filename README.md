# HRDash

Dashboard de **frecuencia cardiaca** a pantalla completa en la terminal.
Lee un sensor BLE estandar (perfil Heart Rate `0x180D`, ej. **Scosche Rhythm/Rhythm+**,
Polar H9/H10, Garmin HRM, Coospo…) y muestra cada segundo:

- numero de FC **gigante** coloreado segun zona de entrenamiento
- **barras de 1 muestra/segundo** a todo el ancho, con el color de cada zona
- FC **max / media / min**, tiempo, %FCmax
- **gasto calorico** en vivo (formula Keytel) segun edad, sexo y peso
- **tiempo y kcal por zona** (Z1–Z5)
- al salir graba la sesion en **FIT compatible con Garmin Connect** (+ CSV espejo)

Corre en **macOS** (CoreBluetooth) y **Linux** (BlueZ) con el mismo codigo.

## Instalacion

```bash
cd fc
uv venv --python 3.12 .venv        # o: python3.12 -m venv .venv
uv pip install -p .venv -e .       # o: .venv/bin/pip install -e .
source .venv/bin/activate          # o agregá alias: alias hrdash=.../fc/.venv/bin/hrdash
```

## Uso

```bash
hrdash                    # pregunta edad [47], sexo [M], peso [70] y abre el dashboard
hrdash --scan             # lista sensores de FC cercanos (¿puesto y encendido?)
hrdash --sensor "Rhythm+" # fuerza un sensor por nombre o direccion/UUID
hrdash --simulate         # demo sin hardware (curva sintetica de FC)
hrdash --age 47 --sex M --weight 72   # sin preguntas
hrdash --duration 30      # headless: 30 s de muestreo + grabacion (para scripts)
hrdash --fresh            # vuelve a preguntar aunque haya config guardada
```

Teclas dentro del dashboard:

| tecla | accion |
|---|---|
| `q` | salir y **grabar FIT + CSV** |
| `r` | reiniciar la sesion (mismos datos) |
| `s` | reconectar el sensor |
| `c` | cambiar edad / sexo / peso en caliente |

Datos y salidas:

- Config persistente: `~/.config/hrdash/config.toml` (edad, sexo, peso, cortes de zona).
- Exportacion: `./exportes/hrdash_<fecha>_<hora>.fit` y `.csv` (`--export-dir` para cambiar).

## Permisos Bluetooth

**macOS**: la primera conexion el sistema pide permiso de Bluetooth para tu terminal
(Terminal/iTerm/Warp/WezTerm…). Si se denegó: *Ajustes del Sistema → Privacidad y
seguridad → Bluetooth* y activarlo. El **Scosche Rhythm+ solo transmite cuando esta
puesto y humedo/pegado a la piel**; suelto sobre la mesa no aparece en el escaneo.
No hace falta emparejarlo manualmente: HRDash se conecta directo (Central BLE).

**Linux**: requiere BlueZ activo (`sudo systemctl enable --now bluetooth`).
Si el escaneo falla por permisos D-Bus: `sudo usermod -aG bluetooth $USER`
(reabrir sesion) o lanzar con el usuario de la sessión de login. `bleak` usa el
adapter por defecto; si hay varios, `BLUETOOTH_ADAPTER=hci1`.

## Subir el FIT a Garmin Connect

Arrastra el `.fit` en <https://connect.garmin.com/activities/upload> (o usa el
importador). Se registra como actividad de entrenamiento con FC por segundo,
media/max/min, calorias y tiempo en zona. El comando hace ademas un *round-trip*
(decodifica el FIT recien escrito) y muestra `round-trip OK` al salir.

## Calculo de calorias (Keytel)

- Hombre: `(-55.0969 + 0.6309·FC + 0.1988·peso + 0.2017·edad) / 4.444` kcal/min
- Mujer:  `(-20.4022 + 0.4472·FC + 0.1213·peso + 0.1574·edad) / 4.444` kcal/min
- FCmax estimada por Tanaka: `208 − 0.7·edad` (fijable con `hrmax` en la config).

## Zonas

| zona | color | %FCmax |
|---|---|---|
| Z1 Recuperacion | azul | 50–60 % |
| Z2 Aerobico | verde | 60–70 % |
| Z3 Tempo | amarillo | 70–80 % |
| Z4 Umbral | naranja | 80–90 % |
| Z5 Maximo | rojo | 90+ % |

Limites editables en `~/.config/hrdash/config.toml` → `zone_pcts`.
