# MusicBee → OBS

Tarjeta horizontal de **640 × 180** con portada, título/artista, álbum, progreso,
tiempo transcurrido y duración. Los metadatos se leen de los controles multimedia
de Windows, la misma fuente que utiliza Music Presence.

## Audio directo de una fuente de OBS

El script se suscribe al audio de **una fuente de OBS que tú seleccionas**.
Lee sus muestras reales a través del callback de captura de `libobs`, después de
los filtros de la fuente. No graba dispositivos de Windows, no captura el audio
del escritorio automáticamente ni cambia la salida o monitorización de MusicBee.
**No requiere VB-CABLE ni SoundCard.**

1. Instala las dependencias **en el mismo Python que tienes seleccionado en OBS**:

   ```console
   python -m pip install winsdk numpy
   ```

2. Conserva tu fuente de OBS que captura MusicBee. El script analiza esa fuente;
   no crea ni configura por su cuenta una captura de aplicación. Para que solo
   entren las ondas de MusicBee, elige una fuente que contenga únicamente su audio.
3. Añade `musicbee_nowplaying.py` en **Herramientas → Scripts** de OBS.
4. En las propiedades del script, elige esa fuente en **Fuente de audio OBS para
   las ondas**. Si acabas de crearla, pulsa **Actualizar fuentes de audio**.
5. Añade una fuente de navegador con `http://localhost:8765/` y dimensiones
   **640 × 180**. El puerto se puede cambiar en las propiedades del script.

El selector ofrece las fuentes con salida de audio de OBS. La fuente debe estar
activa y suministrar audio. La opción **Capturar aunque la fuente esté silenciada
en OBS** está activada por defecto: silenciarla en el mezclador no detiene las
ondas. Si desactivas esa opción, se respeta el silencio del mezclador. Si la fuente
deja de suministrar muestras, se elimina o desactivas las ondas, el visualizador
vuelve al reposo. Si falla
la captura, el script registra el error en OBS y no utiliza una animación falsa
ni busca otra fuente por defecto.

La portada y la información de la canción siguen leyendo MusicBee mediante SMTC;
seleccionar otra fuente de audio no cambia los metadatos.

## Normalización de las ondas

El análisis ajusta automáticamente la ganancia hacia un nivel RMS de 0,12,
con reducción rápida y aumento suave, límite de picos y de amplificación.
El silencio y las señales por debajo de aproximadamente −80 dBFS producen
bandas a cero. Se conserva la energía de los canales, incluso en contrafase.
La forma de las ondas sigue cambiando con la música.

Esta ganancia solo se aplica a una **copia** de las muestras que calcula el
visualizador: **no cambia el volumen audible de MusicBee ni el de la fuente de OBS**.

## 60 FPS

Al cargar los scripts, sus fuentes de navegador locales se configuran con una
frecuencia personalizada de **60 FPS**. Selecciona también **60 FPS** en
**Ajustes → Vídeo** de OBS para que la grabación o emisión conserve esa cadencia.
La onda se anima a 60 FPS y conserva los controles de suavizado, subida y caída.
Consulta [la configuración y los presets](VISUALIZERS.md#activar-los-60-fps-en-obs).

## Detalle y rapidez de las ondas

El análisis usa 128 bandas y comparte 1.024 muestras reales de onda con el script
de visualizadores. Procesa los paquetes completos con FFT solapadas y envía los
cambios por una conexión continua. **Muestras de análisis (FFT)** permite elegir
entre 1.024 y 8.192 (4.096 por defecto).

Ajusta **Suavizado de la forma**, **Tiempo de subida** y **Tiempo de caída** en las
propiedades del script. El suavizado modifica el detalle; los tiempos modifican
la respuesta. Menos milisegundos = más rapidez; 0 = inmediato. Los controles del
overlay y del script de visualizadores son independientes. Consulta los ejemplos
[y las diferencias entre tamaños FFT](VISUALIZERS.md#más-muestras-y-reacción-ajustable).

## Comprobaciones

```console
python -m unittest discover -s tests -v
```

Las pruebas verifican la normalización con señales sintéticas, la copia de
muestras planares, la suscripción exclusiva a la fuente seleccionada, el silencio
y la liberación del callback con una API de OBS simulada. La integración real
requiere ejecutar el script dentro de OBS en Windows 10/11.

## Más estilos

El script separado [obs_visualizers.py](obs_visualizers.py) ofrece **38 presets** y
una galería para compararlos. Por defecto comparte este mismo audio de MusicBee,
incluyendo la captura con la fuente silenciada. También permite conectar sus
colores a la paleta de la portada del overlay. Consulta
[las instrucciones de visualizadores](VISUALIZERS.md).
