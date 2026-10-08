# OBS Visualizers

`obs_visualizers.py` es un segundo script dedicado a los visualizadores. Tiene
ocho estilos: **Barras clásicas, Barras espejo, Ondas suaves, Osciloscopio,
Espectro circular, Anillo fluido, Barras LED y Partículas**.

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

## Comparar estilos

Abre `http://localhost:8766/compare` en tu navegador para ver los ocho estilos
simultáneamente con el mismo audio de MusicBee. Pulsa una tarjeta para ampliarla.
Cuando decidas, selecciona el mismo nombre en **Estilo**, en el script de OBS.

La opción **Comparar con demostración** permite explorar los diseños sin música.
Está desactivada por defecto, se identifica como demostración y solo afecta a esa
galería. El visualizador que añades a OBS sigue utilizando audio real.

La dirección normal sigue el selector de OBS. Si necesitas fijar un estilo en una
fuente concreta, usa `http://localhost:8766/?style=ring`, por ejemplo. Los valores
disponibles son `bars`, `mirror`, `ribbon`, `scope`, `ring`, `orbit`, `dots` y `particles`.

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

Las pruebas cubren el procesamiento, la captura con la fuente silenciada, el
intercambio real de datos entre los dos servidores locales y sus rutas HTTP.
La integración de la fuente de audio real se debe comprobar dentro de OBS en Windows.
