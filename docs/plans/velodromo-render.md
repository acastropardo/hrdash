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

**Estado: hecha** (despues de las fases 0, 2 y 3).

- `track()` integra la curvatura `_kappa(s)` (recta 38 m, clotoide 13,5 m, arco 60 m, por
  curva) por punto medio con subpasos de 2,5 cm sobre la linea de medicion, muestreada cada
  0,5 m (`TRACK_STEP`). R = (13,5 + 60) / pi = 23,3958 m (ojo: las dos clotoides giran lo
  mismo que medio arco; con `2 * TRANS_L` sale R 27,7 y la vuelta no cierra). Medido:
  249,997 m, rumbo continuo (salto max 1,22 grados entre muestras), caja 98 x 47 m.
- Peralte ligado a la curvatura: 12 grados en recta, 45 en el arco (velocidad de diseno
  sqrt(g R tan 45) ~ 54 km/h). `_at()` interpola ahora tambien tangente y peralte.
- `_surf()` mide el ancho sobre la pendiente (`cos`/`sin`); con `tan(bank) * d` la pista
  de 7 m mediria 9,9 a 45 grados. `pts` es la linea de medicion (`AXIS_Q` = 0,20 m).
- Ancho 7 m, cote d'azur de 0,70 m bajo el borde interior (parte de la cubierta, q < 0),
  zona de seguridad llana de 2,5 m con muro al piso. Lineas: negra a 0,20, roja a 0,85,
  azul a max(ancho/3, 2,45). Linea de 200 m a s = 50.
- Gradas a altura fija (`STAND_Z`, borde exterior del arco + barandilla); en las rectas
  un fronton entre barandilla y primera fila. Cercho y lamparas subidos (`ROOF_H`, `LAMP_H`
  derivados de `STAND_Z`); lamparas, cerchos y butacas por metros, no por indice.
- Rider sobre `RIDER_Q` = 0,50 m (encima de la negra) con `_surf`, sin la formula aparte.
- `DECK_STEP` 0,5 -> 0,25: a 45 grados la torsion de quad llegaba a 11 mm (ahora 5,3 mm).
  Tris: 29,7k estaticos + 31k de cubierta.
- Pendiente para la Fase 4: a 38 km/h el ciclista va muy por debajo de la velocidad de
  diseno de la curva y en la chase de curva la camara queda desplazada hacia dentro.

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

**Estado: hecha.**

- Geometria nueva (constantes en `glview.py`): rueda 700c R 0,335, pedalier a 0,28, sillin a
  0,72 m sobre un tubo de 74 grados (0,97 m del suelo), batalla 0,98 m, acoples aero;
  cadera (-0,15, 1,02), torso de 0,52 m a ~15 grados, codo sobre el apoyabrazos, cabeza
  adelantada. Rodilla entre 69 y 139 grados en todo el ciclo (real ~70 / ~145), nunca por
  encima de la cadera ni con la pierna bloqueada; tobillo con +-9 grados segun la fase.
- Primitivas con normales por vertice en numpy (`_tube` con conicidad, `_ellipsoid`,
  `_sphere`, `_torus`, `_disc`), en coordenadas locales (s, h, w). Las piezas rigidas
  (cuadro, ruedas, acoples, torso, brazos, casco) se cachean por color de zona
  (`_rider_static`); por frame solo piernas, bielas, palos y marca de la lenticular, y todo
  se transforma con una multiplicacion de matrices. Resultado: `ArrMesh` en la capa
  `rider`. 3,9k tris en 2,7 ms/frame (POV: 2,2k en 1,3 ms). numpy se importa dentro de
  `_rider` para que el nucleo siga sin dependencias GL.
- Equipacion: torso y mangas del color de zona, culote y pelvis oscuros hasta la rodilla,
  gemelo y antebrazo de piel, calcetin blanco, zapatilla y guantes negros, casco de crono
  en lagrima blanco con visera y franja del color de zona.
- Bici de pista: lenticular trasera con marca que gira, delantera de 3 palos, tubulares,
  plato, bielas, acoples con apoyabrazos, sin frenos.
- POV: la camara va en `HEAD` y el pitch sube de 0,55 a 0,85; con el atleta agachado las
  manos quedaban fuera del cuadro.
- Fuera: `Mesh.wheel`, `Mesh.spoke` y las constantes del muneco anterior.
- `proto.layers_to_json` convierte arrays de numpy a listas (JSON no serializa float32).
- Verificado: capturas (chase recta y curva, lateral lejana y cercana, tres cuartos de
  frente, POV), checks de cinematica de arriba, ventana GLFW real sin `err` a ~25 fps con
  llvmpipe, capas serializables a JSON, smoke FIT OK.

### Fase 4 - movimiento creible

- Linea de rodadura sobre la negra (~0,5 m del borde), que es la de un esfuerzo en solitario.
- Inclinacion: normal del peralte + correccion `atan(v^2 / (g R)) - bank` (en el arco de
  45 deg la velocidad de diseno ronda 54 km/h; por debajo el ciclista va algo "contra" el
  peralte, que es lo real).
- A baja velocidad (< ~25 km/h) baja al cote d'azur/apron y por debajo de ~5 km/h queda
  quieto en el apron (hoy se quedaria parado en un muro de 45 deg).
- Tobillo con angulo segun la fase de biela y un leve balanceo de cadera/hombros.

**Estado: hecha.**

- Inclinacion fisica respecto a la vertical, `tan(phi) = v^2 * kappa / g`, con kappa la
  curvatura en la linea del rider (`_kappa` + desplazamiento lateral), limitada a 55 grados
  y a 1,2 rad/s. Medido en el centro del arco: 60 km/h -> 50 grados, 54 -> 44 (= peralte),
  38 -> 26; en recta 0 aunque el tablon este a 12.
- Linea segun velocidad (`_line_for`), con deriva lateral maxima de 0,6 m/s: >= 34 km/h
  sobre la negra (q 0,50), de 34 a 18 baja a la cote d'azur (q -0,35), de 18 a 10 sale a la
  zona de seguridad (q -1,70) y por debajo se queda ahi. Con mu ~0,6 la velocidad minima en
  el arco de 45 grados es ~27 km/h.
- `_ground()` da el suelo bajo cualquier linea (cubierta peraltada o apron llano); trail y
  sombras usan la normal del suelo, el rider su propio marco inclinado.
- Camara chase en un marco inclinado la mitad que el rider (`CAM_LEAN`); antes usaba la
  normal del peralte y en la curva quedaba desplazada hacia dentro. La POV sigue al rider.
- Verificado: tabla de inclinaciones/lineas de arriba, capturas a 54, 38 y 22 km/h en el
  arco, lateral a 54, recta a 45 y casi parado en el apron; `scene_pista` 3,4 ms/frame;
  ventana real sin `err` (~28 fps llvmpipe); smoke FIT OK.
- No hecho: balanceo de cadera/hombros (el tronco es rigido y cacheado; moverlo obliga a
  transformar esa parte por frame).

### Fase 5 - pulido

- Sombra de contacto suave (disco con alpha radial, requiere blending) en vez de disco negro.
- Interior (infield) con zona de equipos, valla publicitaria en el muro.
- Presets de camara tipo TV (lateral desde el infield, cenital de curva) ademas de chase/POV.

**Estado: hecha.**

- Sombra: capa `shadow` (`ShadowMesh`, formato `3f 2f` = posicion, (radio, fuerza)) con su
  programa y mezcla multiplicativa (`DST_COLOR, ZERO`): oscurece el tablon del shader sin
  taparlo. Una elipse alargada bajo el conjunto y dos de contacto en las ruedas. Sin su
  programa no se dibuja. `FORMATS` reune los formatos propios (deck y shadow): `_fmt_prog`
  los compila una vez por contexto, `_upload` elige formato y `_draw` aplica la mezcla
  (moderngl 5.13 no tiene `VertexArray.extra`: el formato de cada VAO va en `_VA_FMT`).
- Publicidad: la barandilla en paneles de 6 m (`AD_L`) de color plano con un "logo" claro,
  y lonas en el fronton de las rectas.
- Zona interior (`_infield`): suelo deportivo, lineas de una pista polideportiva de 28 x 15 m
  y 8 boxes de equipos (alfombra, biombo, banco, rodillo) en la recta opuesta.
- Camaras (`CAM_MODES`, tecla `v`): chase (la de siempre, con zoom y POV), tv (8 camaras
  fijas en la zona interior, corte a la siguiente al pasar, fovy que sigue la distancia) y
  grua (por dentro y por encima, acompanando). `v` se resuelve en la ventana en los dos
  modos; desde la TUI viaja como contador `cam_cmd` en el snapshot (`proto` lo declara).
- Verificado: capturas offscreen de los tres modos, lateral, lejana y chase; `hrdash
  --simulate` real en tmux + Xvfb con `r` y `v` dos veces (chase -> tv -> grua) y `q`
  (round-trip OK 65/65); ventana sin `err` (~28 fps llvmpipe); `scene_pista` ~3 ms/frame en
  los tres modos; capas serializables a JSON; smoke FIT OK. 31,6k tris estaticos.

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

## Extra: HUD de juego de carreras sobre la pista

**Estado: hecho.**

- `cluster.hud_layers()` reutiliza `dial_static` / `dial_values` / `dial_frame` y las escalas
  de `Cluster._scales`: FC grande + potencia abajo a la izquierda, velocimetro grande +
  cadencia ("cuentarrevoluciones") abajo a la derecha, y una tira arriba con tiempo,
  distancia, vuelta del velodromo (250 m) y kcal. Mismas capas que el cluster
  (`hud_static` por escalas y aspecto, `hud_values` a 1 Hz, `hud_frame` por frame con la
  inercia y el self-test). Por debajo de aspecto 1,5 (vista partida) todo se encoge.
- `_draw()` dibuja la escena y despues el HUD con camara ortografica (`hud_cam`): borra
  solo la profundidad con el color enmascarado. Ojo: en moderngl 5.13 `color_mask` solo
  llega a GL con `fbo.use()`, y `use()` repone el viewport completo; sin restaurar el
  viewport y el scissor del slot, la mascara quedaba en `False` y el HUD no se pintaba.
- `scene_pista` devuelve la camara con un sexto campo (HUD visible); tecla `i` en la
  ventana o en la TUI (contador `hud_cmd`, como `cam_cmd`).
- Coste: `scene_pista` pasa de ~3,1 a ~5,9 ms/frame (las agujas se rehacen por frame).
- Verificado: capturas offscreen (chase, TV, POV), `hrdash --simulate` real con `r`
  (pantalla completa), `3` (vista partida: el HUD queda en su mitad) e `i` (oculto); al
  salir `round-trip OK 64/64`; escenas hr/watts/gauge sin cambios; smoke FIT OK.

## Extra: stuttering en la vista mixta (tablero + pista)

**Estado: hecho.** Reportado en un Mac con GPU real; validado y medido aqui (Xvfb + llvmpipe,
donde no hay vsync, asi que lo del vsync se razona por el codigo y los conteos, no por fps).

- **Causa 1 (la principal)**: `glfw.swap_buffers` estaba dentro del bucle por vistas. En la vista
  mixta las dos vistas comparten ventana, asi que habia 2 intercambios por fotograma: medido
  `swaps/s = 30` con `fps = 15`. Con `swap_interval(1)` cada uno espera a un refresco (tope de 30
  fps), el primero mostraba el fotograma a medias y la segunda mitad se pintaba sobre un buffer
  indefinido. Ahora se intercambia una vez por ventana, despues de dibujar todas las vistas
  (`swaps/s = fps`).
- **Causa 2 (ritmo)**: el bucle dormia hasta 20 ms por fotograma; con vsync eso da 50 fps sobre
  60 Hz y se ve como judder. Ahora el tope es 16 ms (solo evita girar en vacio sin vsync); con
  vsync el propio intercambio marca el ritmo. `ms` del mensaje `fps` ya no incluye la espera
  del vsync.
- **Causa 3 (CPU)**: en la mixta Python gastaba ~13,5 ms por fotograma (presupuesto 16,7 a
  60 Hz), la mitad en reconstruir ~80 palitos de dial (`Mesh.box`) por escena y fotograma.
  `cluster.dial_frame` cachea ahora los palitos por (panel, escala, encendidos, color): la
  geometria es identica (3000 diales aleatorios comparados) y la mixta baja a ~9,5 ms
  (tablero 4,5 -> 2,9, pista 9,0 -> 6,6, con la subida a la GPU incluida).
- Pendiente (no hecho): reutilizar buffers de GPU en vez de crear y liberar uno por capa y
  fotograma (~3 ms), y cachear la capa de la aguja cuando ya convergio.

## Extra: textos de los diales centrados y sin solapes

**Estado: hecho.** Afecta al tablero (`g`/`3`) y al HUD de la pista (mismos `dial_static` /
`dial_values`).

- **Centrado**: `draw_text` centraba con `text_w`, que incluye el hueco tras la ultima letra
  (~0,2 h hacia la izquierda); ahora `align="c"/"r"` usa el trazo real (`ink_w`). Los digitos
  se centran con `digits_extent`: en 7 segmentos un `1` solo pinta el lado derecho de su
  celda, y centrar por celdas dejaba ~0,29 h de hueco en "148".
- **Disposicion** (en unidades de radio): la lectura digital pasa de encima del eje (donde la
  cruzaba la aguja y chocaba con los numeros) a la cuña libre de abajo, con la unidad y el
  nombre/media apilados debajo. Cada texto se ajusta con `_fit` hasta caber en la cara
  (`FACE` 0,93) y, si llega a la zona de palitos, dentro de la cuña (`WEDGE`, de -127 a -53
  grados); se achica solo lo necesario (FRECUENCIA 0,08 -> 0,074).
- **Numeros de la escala**: la caja entera (no solo el centro) debe caber dentro de los palitos
  (`TICK_IN`) y se dejan `NUM_GAP` entre ellos; se mantiene la regla de multiplos (80/120/160,
  100/200/300). Error mio corregido en el camino: proyectar solo en radial no basta, la esquina
  lejana tiene componente perpendicular.
- **Profundidad**: la lectura estaba a z = 0,22 y la perspectiva la corria hacia la unidad en
  los diales de abajo; ahora z = 0,06 como el resto del texto.
- `dial_values` pierde el parametro `dh` (el alto sale del layout).
- Verificado con una auditoria fuera del repo (`audit.py`): 0 fallas en 10.620 numeros de
  2.880 escalas, en todas las lecturas (0-259 ppm, 0-1599 W, 0-99,9 km/h, 0-259 rpm) y en
  todas las filas de abajo; capturas con valores normales, en cero y maximos; HUD; app real
  (Ctrl+C graba, 11/11); smoke FIT OK.
- La unidad del dial de potencia pasa de `%FTP` a `WATTS` (el dial muestra watts; las bandas de color siguen siendo por %FTP), tambien en el HUD (antes `W`).

## Extra: el ciclista pedaleaba al reves

**Estado: hecho.** Reportado como sensacion ("pedalea hacia atras mientras la bici va hacia
adelante"); era un error de signo real.

- `anim["crank"]` y `anim["roll"]` son angulos en el plano de la bici (s = avance, h = altura) y
  crecian. Pedalear o rodar hacia adelante es en sentido horario en ese plano (el pie de arriba
  avanza y luego baja; la parte alta de la rueda avanza), o sea con el angulo decreciendo. Medido
  con el codigo anterior: pedal en lo alto `ds = -0,008`, pedal delante `dh = +0,008`, marca de
  rueda en lo alto `ds = -0,05`; todo al reves. Las marcas de las ruedas (logo de la lenticular y
  palos de la delantera) giraban igual de mal.
- Ahora las dos se restan en `scene_pista`. Medido: pedal en lo alto `ds = +0,008`, delante
  `dh = -0,008`, rueda `ds = +0,05`; en 1 s a 60 rpm la biela da -2 pi y a 30 km/h la rueda
  -24,9 rad (= v / R).
- Verificado con 4 capturas separadas 0,24 s con un reloj de la biela (antihorario antes,
  horario despues), smoke FIT y app real (Ctrl+C graba, 11/11).
- Ojo, aparte: por muestreo, a pocos fps la delantera de 3 palos puede parecer girar al reves
  (efecto rueda de carreta) cuando el giro por fotograma pasa de 60 grados: por encima de
  76 km/h a 60 fps, 57 a 45 fps y 38 a 30 fps.

