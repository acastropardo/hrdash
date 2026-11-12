# Plan: render del velodromo y del ciclista (vista `pista` en OpenGL)

Estado de partida: `c8e8018`. El cluster del tablero (`cluster.py`) se da por casi cerrado;
este plan cubre solo `scene_pista` / `build_static` / `_rider` en `glview.py`.

## Diagnostico (capturas offscreen con Xvfb + Mesa, 1280x720)

Harness en el scratch de la sesion (`shot.py`): construye `scene_pista` con un snapshot
sintetico (38 km/h, 95 rpm), renderiza a un framebuffer standalone y escribe PNG sin PIL.
Vistas: chase en recta, chase en curva, lateral (yaw 1,5), lejana (zoom 6, tilt 0,4), POV.

### Pista: el problema principal es un bug, no de arte

`Mesh.ribbon()` pasa a `_surf()` la coordenada lateral **centrada** (`off +/- half`, en
-5..5), pero `_surf(pt, d)` espera `d` **medida desde el borde interior** (0..10). Toda la
madera y las lineas quedan desplazadas 5 m hacia dentro y con altura `tan(bank) * d`
negativa en la mitad interior. Consecuencias visibles en las capturas:

- hueco negro de 5 m entre la linea blanca exterior y el muro/gradas (el muro si usa
  `_surf(pt, TRACK_W)` bien);
- en curva la mitad interior del tablon se hunde por debajo del apron: el borde azul sale
  dentado y los tablones "en escalera" (z-fighting contra el apron);
- se ve madera con linea roja *dentro* del apron azul (vista lejana);
- el rider (que si usa la convencion correcta, `Q_RIDER + HALF_W`) rueda a la altura
  correcta pero sobre una franja que visualmente es otra cosa.

Ademas, contra un velodromo real:

| aspecto | ahora | real (UCI / referencias) |
|---|---|---|
| longitud | 400 m, curvas = semicirculos R 36 | estandar de pista cubierta y JJOO: **250 m** |
| curva | radio constante | recta -> **clotoide (espiral de Euler)** -> arco -> clotoide; modelo de 250 m: media recta 19 m, transicion 13,5 m, arco 30 m, **R ~ 23,4 m** |
| peralte | 14 deg en curva, 0 en recta | ~**42-45 deg** en curva de 250 m, rectas ~10-13 deg; varia con la curvatura |
| ancho | 10 m (8 calles de atletismo) | **7-8 m** para 250 m |
| lineas | negra 0,24 / roja 0,94 / azul 4,30 | negra (medicion) a **20 cm** del borde interior, roja del sprinter por encima, azul del stayer a **1/3 del ancho o 2,45 m** (lo mayor); cote d'azur debajo de la negra |
| meta | damero blanco/negro | **no es un damero**: banda blanca con linea negra central; las lineas de persecucion son rojas a mitad de cada recta; hay linea de 200 m |
| tablones | 0,24 m de ancho, ~42 cintas | listones de pino de ~4-5 cm: de lejos es textura fina, no rayas de cubierta de barco |

### Ciclista

Captura lateral: se lee como un muneco de palos, no como un ciclista.

- **Codos invertidos**: `_knee(SHO, BAR, ..., bend=-1)` dobla el codo hacia arriba; los
  brazos forman una "V" por encima de la cabeza.
- **Proporciones de bici infantil**: sillin a 0,83 m del suelo (tubo de sillin ~0,56 m);
  cadera->pedal abajo ~0,68 m con piernas de 0,87 m, asi que la rodilla siempre sube a la
  altura del pecho. Torso de 0,37 m (real ~0,50-0,55). Cabeza casi pegada a los hombros.
- **Facetado plano**: `segment()` emite una normal por cara (6 caras), sin articulaciones:
  hay huecos en rodilla/codo/cadera y cada miembro es un prisma de carton.
- **Color**: todo el cuerpo en color de zona (verde) y piel marron; sin maillot, culote,
  calcetines ni casco reconocible.
- **Bici de carretera generica** con 6 radios gruesos; una bici de pista lleva rueda
  lenticular atras y 3/5 palos o lenticular delante, sin frenos ni cambio.
- **Estela**: discos verdes superpuestos bajo la rueda trasera; desde la chase se lee como
  una mancha verde, no como estela.
- **Sin antialiasing**: no se pide MSAA a GLFW; radios y lineas serruchan.

## Fases

### Fase 0 - arreglos (rapido, maximo impacto)

1. `ribbon()`: convertir a `_surf(pt, off +/- half + HALF_W, lift)` y revisar las llamadas
   de `build_static` (base, tablones, lineas, meta) con la misma convencion.
2. Codo: invertir el signo de `bend` (o del termino perpendicular en `_knee`) y afirmar en
   el harness que el codo queda por debajo de la recta hombro-mano.
3. MSAA: `glfw.window_hint(glfw.SAMPLES, 4)` antes de crear la ventana y
   `ctx.enable(moderngl.MULTISAMPLE)`; fallback sin MSAA si falla (nunca tumbar el hijo).
4. Meta UCI (banda blanca + linea negra) en vez de damero; linea de 200 m y lineas rojas
   de persecucion.
5. Estela: o se quita, o pasa a ser una cinta fina pegada al tablon que se desvanece.

**Estado: hecha.**

- `ribbon()` suma `HALF_W` antes de llamar a `_surf`. Al corregirlo aparecio un segundo
  defecto que el primero tapaba: la cinta base de 10 m se tuerce en la rampa de peralte
  (quad no plano, la diagonal se separa cm de la superficie) y asomaba entre tablones y
  lineas como un escalonado. La base ahora va partida por tablon (el ultimo recortado al
  borde). Coste: 73k -> 107k tris estaticos (se suben una vez; la Fase 2 los elimina).
- `_knee()` es una rotacion por `bend * a`; con `bend=-1` antes era una reflexion. Codo a
  0,76 m (antes 1,36, por encima del casco), la rodilla no cambia.
- `glfw.SAMPLES = 4` con `_mkwin()`, que repite sin multisample si el driver no lo da.
- Meta: banda blanca de 0,72 m con linea negra de 4 cm; linea de 200 m; lineas rojas de
  persecucion a mitad de cada recta (medio ancho). `_cross()` dibuja cualquier marca
  transversal a `s` metros de meta y `_straight_mids()` localiza las rectas.
- Estela: cinta de 4 cm sobre el tablon que empieza detras de la rueda trasera y se
  desvanece.
- Verificado: capturas antes/despues (chase recta y curva, lateral, lejana, meta, POV),
  checks numericos (tablon >= DECK, base cubre 0..TRACK_W, codo bajo la recta
  hombro-mano, centros de recta en 156 y 356 m), ventana GLFW real con MSAA bajo Xvfb
  (`win` 1920x1080 sin `err`; ~22 fps con llvmpipe, igual que antes) y smoke FIT OK.
- Sigue pendiente lo de la Fase 3: con el codo arreglado se ve mejor que las proporciones
  (sillin bajo, torso corto, rodilla a la altura de la cadera) son el problema del ciclista.

### Fase 1 - geometria de un 250 m real

- `track()` parametrizado: media recta 19 m, clotoide 13,5 m, arco 30 m (R ~ 23,4),
  muestreo por longitud de arco (la fraccion sigue siendo distancia real).
- Peralte continuo ligado a la curvatura: 12 deg en recta -> 45 deg en el arco, sin la
  rampa coseno aparte (la clotoide ya da la transicion suave).
- Ancho 7 m, cote d'azur + apron plano, lineas UCI con las medidas de la tabla.
- `TRACK_LEN` pasa a 250; la posicion sigue siendo `dist % TRACK_LEN`. Las vueltas de la
  TUI (`lap_m`) son independientes y no cambian.
- Muro y gradas se apoyan en el borde exterior real (con 45 deg queda a ~5 m de altura,
  hay que subir gradas y cerchos).

### Fase 2 - tablon por shader

- Programa aparte para la cubierta con atributo `(s, q)` (avance, lateral). El fragment
  shader dibuja los listones de 4-5 cm con tono por liston (hash de `floor(q/w)`), juntas
  finas y las lineas pintadas con `fwidth` (antialiasing analitico, sin z-fighting ni
  `lift`). Reemplaza ~45 cintas por una sola malla.
- El charco de luz de las lamparas tambien en el shader (hoy se hornea por vertice).
- No toca el `VATTR` del resto de escenas: el cluster sigue con su programa.

**Estado: hecha** (sobre la geometria actual de 400 m; la Fase 1 solo cambia `track()`).

- Capa nueva `deck` (`DeckMesh`, formato `DECK_VATTR` = posicion, normal, `(s, q)`,
  calidez) construida por `build_deck()`: filas cada metro, columnas cada `DECK_STEP`
  (0,5 m), normales por vertice. Sustituye las ~41 cintas de tablon, sus bases y las 5
  cintas de lineas: 107k -> 36k tris estaticos + 16k de cubierta.
- `DECK_FS` (generado por `_deck_fs()` con las constantes de Python incrustadas): listones
  de 5 cm con testas escalonadas cada 4 m, tono por liston que se apaga con la distancia,
  juntas y lineas pintadas filtradas con un filtro de caja exacto sobre `fwidth` (no hay
  moire ni parpadeo de lejos, ni z-fighting porque no hay capas superpuestas).
- La luz del pabellon vive en `SHADE` y la comparten `FS` y `DECK_FS`.
- `_upload()` elige programa y formato por capa (`mesh.fmt`); el programa de la cubierta se
  compila una vez por contexto (`_deck_prog`). Si no compila emite `warn` y la cubierta cae
  a `to_mesh()` (madera lisa sin lineas) con el programa general: la ventana sigue viva.
- `proto.layers_to_json` anade `fmt` a las capas con otro formato de vertice.
- Las marcas transversales (meta, 200 m, persecucion) siguen como geometria con lift 12 mm.
- Verificado: capturas (chase recta, curva, lateral, lejana, meta, POV y respaldo sin
  shader), torsion maxima de quad de cubierta 6,7 mm (< lift de las marcas), cubierta en
  q 0..10 y s 0..400, ventana GLFW real sin `warn` ni `err` a ~26 fps con llvmpipe (antes
  ~22), escenas `hr`/`watts`/`gauge` construyen igual, smoke FIT OK.
- Ojo: las sombras de contacto del ciclista van a 6 mm sobre la superficie, casi la
  torsion maxima; si en la rampa de peralte asoman, bajar `DECK_STEP` a 0,25.

### Fase 3 - ciclista nuevo

- Proporciones de un atleta de ~1,75 m en bici de pista: rueda 700c (R 0,335), pedalier a
  ~0,29 m, biela 0,165, sillin a ~0,72 m del pedalier sobre un tubo de ~74 deg, manillar
  10-12 cm por debajo del sillin; torso ~0,52 m a 15-25 deg de la horizontal (postura de
  persecucion), cabeza adelantada.
- Malla: cilindros de 10-12 lados con **normales por vertice** (suave), esferas en
  articulaciones (cadera, rodilla, tobillo, hombro, codo, cabeza), torso como tronco de
  seccion eliptica.
- Equipacion: mono oscuro con paneles/mangas del color de zona, culote hasta la rodilla,
  piel solo en antebrazo y gemelo, calcetin y zapatilla, casco de crono en lagrima. El
  color de zona sigue informando sin pintar todo el cuerpo.
- Bici de pista: lenticular trasera, 3 palos delantera, cuadro de perfil aero, plato
  grande, sin frenos. Las ruedas lenticulares ademas eliminan el serrucho de los radios;
  el giro se lee con un logo/marca en el disco.

### Fase 4 - movimiento creible

- Linea de rodadura sobre la negra (~0,5 m del borde), que es la de un esfuerzo en solitario.
- Inclinacion: normal del peralte + correccion `atan(v^2 / (g R)) - bank` (en el arco de
  45 deg la velocidad de diseno ronda 54 km/h; por debajo el ciclista va algo "contra" el
  peralte, que es lo real).
- A baja velocidad (< ~25 km/h) baja al cote d'azur/apron y por debajo de ~5 km/h queda
  quieto en el apron (hoy se quedaria parado en un muro de 45 deg).
- Tobillo con angulo segun la fase de biela y un leve balanceo de cadera/hombros.

### Fase 5 - pulido

- Sombra de contacto suave (disco con alpha radial, requiere blending) en vez de disco negro.
- Interior (infield) con zona de equipos, valla publicitaria en el muro.
- Presets de camara tipo TV (lateral desde el infield, cenital de curva) ademas de chase/POV.

## Como se verifica

- Harness offscreen (fuera del repo) con Xvfb: las 5 capturas de arriba antes/despues de
  cada fase. Afirmaciones numericas: sin hueco entre linea exterior y muro; altura del
  tablon >= `DECK` en todo el ancho; codo por debajo de la recta hombro-mano; rodilla nunca
  por encima de la cadera; peralte maximo 45 deg y continuo (sin saltos entre muestras).
- Presupuesto de frame: medir tris estaticos y por frame; el rider no debe pasar de ~3000
  tris y el frame debe seguir por debajo de 20 ms (`fps_check`).
- Smoke del repo tras cada fase:
  `XDG_CONFIG_HOME=/tmp/hrdash_xdg .venv/bin/hrdash --simulate --duration 3 --export-dir /tmp/hrdash_check`.

## Referencias

- Stanoev, "On technical considerations of UCI-regulated velodrome track design"
  (arXiv 2207.13556): tabla de radios/anchos por longitud (UCI art. 3.6.095).
- "On modelling bicycle power for velodromes" (arXiv 2005.04691): parametrizacion de 250 m
  con espiral de Euler (19 / 13,5 / 30 m, R ~ 23,4 m).
- Wikipedia, "Velodrome": peralte ~45 deg en 250 m, ~32 deg en 333 m.
- Guia de dimensiones de WA (dlgsc.wa.gov.au): linea de medicion a 20 cm, linea del stayer a
  1/3 del ancho o 2,45 m, lineas de persecucion a mitad de recta.
- Pendiente de confirmar en el reglamento UCI (cap. VI): cota exacta de la linea roja y
  ancho de la banda de meta.
