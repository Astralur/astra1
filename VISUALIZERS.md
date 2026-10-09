# OBS Visualizers

`obs_visualizers.py` es un segundo script dedicado a los visualizadores. Tiene
**38 presets**: los ocho originales y 30 nuevos diseños de barras, ondas,
osciloscopios, anillos, radar, espiral, mosaico y partículas.

## Usarlo con el mismo audio de MusicBee

1. Actualiza `musicbee_nowplaying.py` y cárgalo en OBS. Selecciona tu fuente que
   contiene el audio de MusicBee en sus propiedades. **Capturar aunque la fuente
   esté silenciada en OBS** viene activado: puedes silenciarla en el mezclador y
   las ondas seguirán recibiendo las muestras reales. No cambia la salida de MusicBee.
2. Carga también `obs_visualizers.py` en **Herramientas → Scripts**.
3. Deja **Audio → MusicBee (mismo audio que el overlay)**. El puerto del overlay
   de MusicBee es **8765** por defecto; si lo cambiaste, actualiza ese campo.
4. Añade otra **Fuente de navegador** con `http://localhost:8766/` y tamaño
   **900 × 300**. Para los estilos circulares también puedes usar **600 × 600**.
5. Cambia **Estilo** en las propiedades del nuevo script. La fuente se actualiza
   sin recargarla. Ajusta colores, intensidad, suavizado, grosor, cantidad de bandas
   y brillo. El fondo es transparente por defecto.

El nuevo script obtiene el espectro, la onda y el nivel ya normalizados del overlay
de MusicBee. Ambos analizan la misma señal: no se abre otra captura de dispositivos
ni se necesita VB-CABLE. El osciloscopio usa muestras reales, no una onda sintética.

## Más muestras y reacción ajustable

**Actualiza los dos scripts**, porque el formato de audio compartido ha cambiado.
El análisis procesa todos los paquetes recibidos de la fuente a la frecuencia
original de OBS (por ejemplo, 44,1 o 48 kHz). Usa FFT solapadas con avances de
hasta 512 muestras, conserva los ataques entre ventanas y envía **128 bandas** y
**1.024 muestras reales de onda**. Las actualizaciones llegan por conexión continua,
sin el sondeo HTTP anterior de 30 veces por segundo. La visualización principal
se dibuja a la frecuencia de animación del navegador; las miniaturas, hasta 30 FPS.

En las propiedades de **cada script** puedes ajustar:

| Control | Qué cambia |
| --- | --- |
| **Suavizado de la forma** | Mezcla muestras/bandas vecinas: 0 conserva todo el detalle y 1 suaviza más. No añade espera entre cuadros. |
| **Tiempo de subida (ms)** | Rapidez al aparecer un ataque. Menos tiempo = respuesta más rápida; 0 = inmediato. |
| **Tiempo de caída (ms)** | Tiempo que tarda en volver al reposo. Se ajusta independientemente de la subida; 0 = inmediato. |
| **Muestras de análisis (FFT)** | 1.024, 2.048, 4.096 o 8.192. Una ventana mayor distingue mejor las frecuencias graves, pero extiende la respuesta en el tiempo y consume más CPU. |

En el modo de audio compartido, cambia **Muestras de análisis (FFT)** en
`musicbee_nowplaying.py`; en el modo independiente, en `obs_visualizers.py`.
La ventana por defecto es de 4.096 muestras (unos 85 ms a 48 kHz), con análisis
solapado para seguir actualizando durante esa ventana. Esto no cambia la frecuencia
de muestreo de OBS ni el volumen audible.

Para empezar: **suavizado 0,2**, **subida 18 ms**, **caída 140 ms**. Para una reacción
más seca, prueba subida 0–10 ms y caída 40–80 ms. Para una onda más relajada, prueba
subida 40–80 ms y caída 250–500 ms. El osciloscopio conserva las muestras recientes
sin promediarlas entre cuadros; los tiempos controlan su amplitud de entrada/salida.

Si el equipo se satura, la cola está limitada a 32 paquetes para evitar que se
acumule retraso. El diagnóstico `analysis` de `/state.json` y `/spectrum.json`
incluye la frecuencia de muestreo, tamaño FFT, muestras procesadas, ventanas
analizadas y paquetes descartados por sobrecarga.

## Comparar estilos

Abre `http://localhost:8766/compare` en tu navegador para ver los 38 estilos
con el mismo audio de MusicBee. Filtra por familia y pulsa una tarjeta para ampliarla.
Solo se dibujan las
tarjetas visibles para limitar el consumo al comparar tantos estilos.
Cuando decidas, selecciona el mismo nombre en **Estilo**, en el script de OBS.

La opción **Comparar con demostración** permite explorar los diseños sin música.
Está desactivada por defecto, se identifica como demostración y solo afecta a esa
galería. El visualizador que añades a OBS sigue utilizando audio real.

La dirección normal sigue el selector de OBS. Si necesitas fijar un estilo en una
fuente concreta, usa `http://localhost:8766/?style=ring`, por ejemplo. Los
identificadores de los 38 presets aparecen en `PRESETS`, al principio del script;
por ejemplo, `scope_trail`, `ring_spiral` o `particles_constellation`.

## Modo independiente

También puedes cambiar **Audio → Una fuente de OBS (independiente)** y elegir una
fuente directamente. Ese modo funciona sin cargar el overlay de MusicBee, necesita
`numpy` en el Python de OBS y permite activar/desactivar la normalización. En modo
independiente se respeta el silencio de la fuente; la captura aun estando silenciada
se configura en el overlay de MusicBee.

Los dos scripts usan puertos distintos y pueden coexistir. Si no llega audio,
la fuente se silencia o se interrumpe la conexión con el overlay, las animaciones
vuelven al reposo. No hay captura alternativa del escritorio ni demostración automática.

## Validación

```console
python -m unittest discover -s tests -v
```

Las pruebas cubren el procesamiento, la captura con la fuente silenciada, la
conservación de paquetes, ataques al comienzo de un lote, las ventanas FFT,
el intercambio continuo de datos entre ambos servidores locales y sus rutas HTTP.
La integración de la fuente de audio real se debe comprobar dentro de OBS en Windows.

La comprobación opcional de navegador verifica los 38 diseños, filtros, 1.024
muestras de onda (1.023 segmentos), transparencia y los controles de respuesta:

```console
python -m pip install playwright
python -m playwright install chromium
python tests/check_visualizers_browser.py
```

Puedes usar un Chromium ya instalado indicando su ruta en `CHROMIUM_PATH`.
