"""
OBS Visualizers — visualizadores independientes para una fuente de audio de OBS.
Windows 10/11, Python configurado en OBS y pip install numpy.
Fuente de navegador: http://localhost:8766/ (900 × 300, fondo transparente).
Galería para comparar los estilos: http://localhost:8766/compare
Por defecto usa el mismo audio que el overlay MusicBee NowPlaying.
También permite una fuente directa de OBS. No utiliza VB-CABLE ni cambia salidas.
"""
import ctypes
import json
import math
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from urllib.request import build_opener, ProxyHandler

import obspython as obs

BANDS = 128
WAVE_POINTS = 1024
PRESETS = {
    "bars": "Barras clásicas",
    "mirror": "Barras espejo",
    "ribbon": "Ondas suaves",
    "scope": "Osciloscopio",
    "ring": "Espectro circular",
    "orbit": "Anillo fluido",
    "dots": "Barras LED",
    "particles": "Partículas",
    "bars_peaks": "Barras con picos",
    "bars_horizontal": "Barras horizontales",
    "bars_split": "Barras enfrentadas",
    "bars_edges": "Barras desde los bordes",
    "bars_center": "Barras hacia el centro",
    "bars_rounded": "Píldoras",
    "bars_thin": "Líneas finas",
    "bars_steps": "Escalones",
    "ribbon_outline": "Contorno de onda",
    "ribbon_layers": "Ondas en capas",
    "ribbon_single": "Montaña de frecuencias",
    "ribbon_dual": "Ondas cruzadas",
    "ribbon_tunnel": "Túnel de ondas",
    "scope_double": "Osciloscopio doble",
    "scope_fill": "Osciloscopio relleno",
    "scope_dots": "Osciloscopio punteado",
    "scope_trail": "Estela de osciloscopio",
    "scope_xy": "Órbita de la señal",
    "ring_inward": "Anillo hacia dentro",
    "ring_double": "Doble anillo",
    "ring_dots": "Círculo de puntos",
    "ring_polygon": "Polígono reactivo",
    "ring_spiral": "Espiral espectral",
    "ring_flower": "Flor sonora",
    "ring_sun": "Rayos de sonido",
    "orbit_radar": "Radar sonoro",
    "orbit_ripples": "Ecos circulares",
    "grid_heat": "Mosaico espectral",
    "particles_fountain": "Fuente de partículas",
    "particles_constellation": "Constelación"
}
_cfg = {
    "audio_mode": "musicbee", "musicbee_port": 8765,
    "analysis_size": 4096,
    "audio_source": "", "port": 8766, "style": "ribbon",
    "visualizer": True, "normalize": True,
    "color_a": 0xFFFFE156, "color_b": 0xFFFF72AD, "background_color": 0xFF181008,
    "background": False, "glow": True, "link_colors": False,
    "attack_ms": 18.0, "release_ms": 140.0,
    "intensity": 1.0, "smoothing": .2, "thickness": 3.0, "density": 48,
}
_lock = threading.Lock()
_audio_updated = threading.Condition(_lock)
_audio_seq = 0
_stop = threading.Event()
_audio_thread = None
_settings_ref = None
_pending_style = None
_pending_color_link = None
_cover_cache = None
_last_browser_scan = 0.0
_httpd = None
_httpd_port = None
_audio_ok = False
_audio_error = None
_audio_source_name = ""
_audio = {"bands": [0.0] * BANDS, "wave": [0.0] * WAVE_POINTS, "level": 0.0, "stamp": 0.0}


class _SpectrumProcessor:
    """PCM continuo, FFT solapada y normalización exclusiva del visualizador."""
    def __init__(self, np, rate=44100, n=4096):
        self.np, self.rate, self.n = np, rate, n
        self.hop = 512
        self.win = np.hanning(n).astype(np.float32)[:, None]
        self.buf = np.zeros((n, 2), dtype=np.float32)
        self.gain = None
        self.wave = [0.0] * WAVE_POINTS
        self.level = 0.0
        self.processed_frames = 0
        self.fft_windows = 0
        edges = np.geomspace(40, min(20000, rate * .49), BANDS + 1)
        freqs = np.fft.rfftfreq(n, 1.0 / rate)
        self.idx = [np.flatnonzero((freqs >= edges[i]) & (freqs < edges[i + 1])) for i in range(BANDS)]
        for i, bins in enumerate(self.idx):
            if not len(bins):
                self.idx[i] = np.array([int(np.argmin(np.abs(freqs - edges[i])))])
        self.tilt = np.linspace(0, .22, BANDS)

    def reset(self):
        self.wave = [0.0] * WAVE_POINTS
        self.level = 0.0
        self.buf.fill(0)
        self.gain = None

    def process(self, data, normalize=True):
        np = self.np
        if not len(data):
            self.reset()
            return [0.0] * BANDS
        data = np.nan_to_num(np.asarray(data, dtype=np.float32), nan=0, posinf=0, neginf=0)
        self.processed_frames += len(data)
        if self.buf.shape[1] != data.shape[1]:
            self.buf = np.zeros((self.n, data.shape[1]), dtype=np.float32)
            self.gain = None
        rms = float(np.sqrt(np.mean(data * data)))
        if rms <= .0001:
            self.reset()
            return [0.0] * BANDS
        peak = float(np.max(np.abs(data)))
        desired = min(100.0, .12 / rms, .98 / peak)
        if not normalize:
            self.gain = 1.0
        elif self.gain is None:
            self.gain = desired
        else:
            tau = .05 if desired < self.gain else .6
            self.gain += (desired - self.gain) * (1 - math.exp(-len(data) / self.rate / tau))
        if normalize:
            self.gain = min(self.gain, .98 / peak)
        samples = data * self.gain
        self.level = min(1.0, float(np.sqrt(np.mean(samples * samples))) * 4)
        mag = np.zeros(self.n // 2 + 1, dtype=np.float32)
        # Analizar TODOS los bloques recibidos, no únicamente la cola de la señal.
        # Se conservan los picos entre ventanas del mismo lote para no perder ataques.
        for offset in range(0, len(samples), self.hop):
            block = samples[offset:offset + self.hop]
            count = len(block)
            self.buf[:-count] = self.buf[count:]
            self.buf[-count:] = block
            fft = np.fft.rfft(self.buf * self.win, axis=0)
            current = np.sqrt(np.mean(np.abs(fft) ** 2, axis=1)) / (self.n / 4)
            np.maximum(mag, current, out=mag)
            self.fft_windows += 1
        # Enviar 1024 muestras reales, sin interpolarlas desde una onda pequeña.
        channel = int(np.argmax(np.mean(self.buf * self.buf, axis=0)))
        signal = self.buf[:, channel]
        limit = self.n - WAVE_POINTS
        crossings = np.flatnonzero((signal[:limit + 1] <= 0) & (signal[1:limit + 2] > 0)) if limit else []
        start = int(crossings[-1]) if len(crossings) else limit
        self.wave = np.clip(signal[start:start + WAVE_POINTS], -1, 1).astype(float).round(5).tolist()
        out = []
        for i, bins in enumerate(self.idx):
            db = 20 * np.log10(float(mag[bins].max()) + 1e-9)
            out.append(round(min(1.0, max(0.0, (db + 72) / 52 + self.tilt[i])), 4))
        return out


# Contrato de libobs: obs.h (obs_source_audio_capture_t) y media-io/audio-io.h.
# El callback recibe float32 planar al sample rate de OBS, después de los filtros.
class _ObsAudioData(ctypes.Structure):
    _fields_ = [('data', ctypes.c_void_p * 8), ('frames', ctypes.c_uint32),
                ('timestamp', ctypes.c_uint64)]


class _ObsAudioInfo(ctypes.Structure):
    _fields_ = [('samples_per_sec', ctypes.c_uint32), ('speakers', ctypes.c_int32)]


_AUDIO_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.POINTER(_ObsAudioData), ctypes.c_bool)


class _ObsAudioApi:
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('La captura de fuentes debe ejecutarse dentro de OBS en Windows.')
        kernel = ctypes.WinDLL('kernel32')
        kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel.GetModuleHandleW.restype = ctypes.c_void_p
        handle = kernel.GetModuleHandleW('obs.dll') or kernel.GetModuleHandleW('libobs.dll')
        if not handle:
            raise RuntimeError('No se encuentra la biblioteca de OBS ya cargada.')
        # CDLL libera el GIL durante RemoveCallback: evita bloquear el hilo de audio
        # cuando otro callback necesita entrar en Python. Nunca cargar otra copia de OBS.
        self.lib = ctypes.CDLL('obs.dll', handle=handle)
        signatures = {
            'obs_get_source_by_name': (ctypes.c_void_p, [ctypes.c_char_p]),
            'obs_source_release': (None, [ctypes.c_void_p]),
            'obs_source_removed': (ctypes.c_bool, [ctypes.c_void_p]),
            'obs_source_get_output_flags': (ctypes.c_uint32, [ctypes.c_void_p]),
            'obs_source_get_name': (ctypes.c_char_p, [ctypes.c_void_p]),
            'obs_get_audio_info': (ctypes.c_bool, [ctypes.POINTER(_ObsAudioInfo)]),
            'obs_source_add_audio_capture_callback': (None, [ctypes.c_void_p, _AUDIO_CALLBACK, ctypes.c_void_p]),
            'obs_source_remove_audio_capture_callback': (None, [ctypes.c_void_p, _AUDIO_CALLBACK, ctypes.c_void_p]),
        }
        for name, (restype, argtypes) in signatures.items():
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = restype, argtypes
            setattr(self, name, fn)

    def audio_info(self):
        info = _ObsAudioInfo()
        if not self.obs_get_audio_info(ctypes.byref(info)):
            raise RuntimeError('OBS todavía no tiene el audio inicializado.')
        channels = info.speakers or 2
        if not 8000 <= info.samples_per_sec <= 192000 or not 1 <= channels <= 8:
            raise RuntimeError('Formato de audio de OBS no compatible.')
        return info.samples_per_sec, channels


class _ObsAudioTap:
    """Retener una fuente, copiar sus muestras y retirar el callback antes de liberarla."""
    def __init__(self, api, name):
        self.api, self.requested = api, name
        self.source = None
        self.active = False
        self.registered = False
        self.error = None
        self.packets = queue.Queue(maxsize=32)
        self.dropped_packets = 0
        self.callback = _AUDIO_CALLBACK(self._capture)
        self.rate, self.channels = api.audio_info()
        try:
            self.source = api.obs_get_source_by_name(name.encode('utf-8'))
            if not self.source:
                raise RuntimeError(f'No se encuentra la fuente de OBS "{name}".')
            if api.obs_source_removed(self.source):
                raise RuntimeError(f'La fuente "{name}" se ha eliminado de OBS.')
            if not api.obs_source_get_output_flags(self.source) & 2:  # OBS_SOURCE_AUDIO
                raise RuntimeError(f'La fuente "{name}" no tiene salida de audio.')
            self.active = True
            api.obs_source_add_audio_capture_callback(self.source, self.callback, None)
            self.registered = True
        except Exception:
            self.close()
            raise

    def _capture(self, _, source, audio, muted):
        # No ejecutar FFT ni llamar a OBS desde su hilo de audio. Copiar antes
        # de que OBS reutilice los buffers; descartar solo si se llena la cola.
        if not self.active or source != self.source or not audio:
            return
        try:
            data = audio.contents
            frames = data.frames
            if not 0 < frames <= self.rate * 2:
                return
            planes = None if muted else tuple(
                ctypes.string_at(data.data[c], frames * 4) if data.data[c] else bytes(frames * 4)
                for c in range(self.channels))
            try:
                self.packets.put_nowait(planes)
            except queue.Full:
                try:
                    self.packets.get_nowait()
                    self.dropped_packets += 1
                except queue.Empty:
                    pass
                try:
                    self.packets.put_nowait(planes)
                except queue.Full:
                    pass
        except Exception as e:
            self.error = str(e)

    def read(self, np):
        pending = [self.packets.get(timeout=.025)]
        while True:
            try:
                pending.append(self.packets.get_nowait())
            except queue.Empty:
                break
        # El silencio explícito marca una discontinuidad. Conservar toda la señal
        # posterior a ella, en lugar de descartar cada paquete salvo el último.
        if pending[-1] is None:
            return None
        last_mute = max((i for i, planes in enumerate(pending) if planes is None), default=-1)
        chunks = [np.column_stack([np.frombuffer(p, dtype=np.float32) for p in planes])
                  for planes in pending[last_mute + 1:]]
        return np.concatenate(chunks, axis=0)


    def close(self):
        self.active = False
        if self.registered:
            self.api.obs_source_remove_audio_capture_callback(self.source, self.callback, None)
            self.registered = False
        if self.source:
            self.api.obs_source_release(self.source)
            self.source = None


def _publish_audio(bars, ready=False, source='', error=None, wave=None, level=0.0, analysis=None):
    global _audio_ok, _audio_source_name, _audio_error, _audio_seq
    with _audio_updated:
        _audio_ok, _audio_source_name, _audio_error = ready, source, error
        _audio.update(bands=list(bars), wave=list(wave) if wave is not None else [0.0] * WAVE_POINTS,
                      level=level, stamp=time.monotonic(), analysis=dict(analysis or {}))
        _audio_seq += 1
        _audio_updated.notify_all()


_local_http = build_opener(ProxyHandler({}))


def _read_musicbee(port):
    if port == _httpd_port:
        raise RuntimeError('El puerto de MusicBee debe ser distinto del puerto de visualizadores.')
    with _local_http.open(f'http://127.0.0.1:{port}/spectrum.json', timeout=.75) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise RuntimeError('La respuesta de MusicBee es demasiado grande.')
    _consume_musicbee(json.loads(raw))


def _consume_musicbee(data):
    source = str(data.get('source') or 'MusicBee')[:256]
    age = float(data.get('age', 0))
    if not math.isfinite(age):
        raise RuntimeError('MusicBee devolvió una edad de audio no válida.')
    if data.get('real') is not True or data.get('enabled') is not True or age > .5:
        _publish_audio([0.0] * BANDS, source=source, error=data.get('error'))
        return
    bands, wave = data.get('bars'), data.get('wave')
    if not isinstance(bands, list) or not 8 <= len(bands) <= 128 or not isinstance(wave, list) or len(wave) != WAVE_POINTS:
        raise RuntimeError('Actualiza musicbee_nowplaying.py para compartir el espectro y las ondas.')
    def numbers(values, low, high):
        out = [float(v) for v in values]
        if not all(math.isfinite(v) for v in out):
            raise RuntimeError('MusicBee devolvió muestras de audio no válidas.')
        return [min(high, max(low, v)) for v in out]
    level = numbers([data.get('level', 0)], 0, 1)[0]
    _publish_audio(numbers(bands, 0, 1), True, source, wave=numbers(wave, -1, 1), level=level, analysis=data.get("analysis", {}))


def _stream_musicbee(port):
    if port == _httpd_port:
        raise RuntimeError("El puerto de MusicBee debe ser distinto del puerto de visualizadores.")
    with _local_http.open(f"http://127.0.0.1:{port}/audio-events", timeout=.75) as response:
        while not _stop.is_set():
            with _lock:
                if not _cfg["visualizer"] or _cfg["audio_mode"] != "musicbee" or _cfg["musicbee_port"] != port:
                    return
            line = response.readline(65537)
            if not line:
                raise RuntimeError("Se ha interrumpido la conexión de audio con MusicBee.")
            if len(line) > 65536:
                raise RuntimeError("La respuesta de MusicBee es demasiado grande.")
            if line.startswith(b"data: "):
                _consume_musicbee(json.loads(line[6:]))


def _audio_worker():
    """Compartir la señal del overlay MusicBee o leer una fuente explícita de OBS."""
    zero, tap, np, api = [0.0] * BANDS, None, None, None
    processor = None
    last_packet, inspected, last_error = 0.0, 0.0, None
    try:
        while not _stop.is_set():
            try:
                with _lock:
                    enabled, requested, normalize = _cfg['visualizer'], _cfg['audio_source'], _cfg['normalize']
                    mode, musicbee_port = _cfg['audio_mode'], _cfg['musicbee_port']
                    analysis_size = _cfg['analysis_size']
                if not enabled:
                    if tap:
                        tap.close()
                        tap = None
                    _publish_audio(zero)
                    _stop.wait(.1)
                    continue
                if mode == 'musicbee':
                    if tap:
                        tap.close()
                        tap = None
                    _stream_musicbee(musicbee_port)
                    last_error = None
                    continue
                if not requested:
                    if tap:
                        tap.close()
                        tap = None
                    _publish_audio(zero, error=None if not enabled else 'Selecciona una fuente de audio de OBS para las ondas.')
                    _stop.wait(.1)
                    continue
                if np is None:
                    import numpy as np
                if api is None:
                    api = _ObsAudioApi()
                if tap and tap.requested != requested:
                    tap.close()
                    tap = None
                if tap is None:
                    tap = _ObsAudioTap(api, requested)
                    processor = _SpectrumProcessor(np, rate=tap.rate, n=analysis_size)
                    last_packet = inspected = time.monotonic()
                    _publish_audio(zero, source=requested)
                if processor.n != analysis_size:
                    processor = _SpectrumProcessor(np, rate=tap.rate, n=analysis_size)
                now = time.monotonic()
                if now - inspected >= .5:
                    if api.obs_source_removed(tap.source):
                        raise RuntimeError(f'La fuente "{requested}" se ha eliminado de OBS.')
                    if api.audio_info() != (tap.rate, tap.channels):
                        tap.close()
                        tap = None
                        continue
                    inspected = now
                if tap.error:
                    raise RuntimeError(tap.error)
                try:
                    data = tap.read(np)
                except queue.Empty:
                    if now - last_packet >= .2:
                        processor.reset()
                        _publish_audio(zero, source=requested)
                    continue
                if data is None:
                    processor.reset()
                    bars = zero
                else:
                    bars = processor.process(data, normalize=normalize)
                _publish_audio(bars, True, requested, wave=processor.wave, level=processor.level,
                               analysis={"sample_rate": tap.rate, "fft_size": processor.n,
                                         "processed_frames": processor.processed_frames,
                                         "fft_windows": processor.fft_windows, "dropped_packets": tap.dropped_packets})
                last_packet = time.monotonic()
                last_error = None
            except Exception as e:
                if tap:
                    tap.close()
                    tap = None
                _publish_audio(zero, error=str(e))
                if str(e) != last_error:
                    obs.script_log(obs.LOG_WARNING, f'OBS Visualizers (fuente OBS): {e}')
                    last_error = str(e)
                _stop.wait(1)
    finally:
        if tap:
            tap.close()
        with _lock:
            error = _audio_error
        _publish_audio(zero, error=error)



OVERLAY_HTML = r"""<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Visualizadores · OBS</title>
<style>
:root{color-scheme:dark;--a:#56e1ff;--b:#ad72ff;--text:#edf4fa;--muted:#9faec2;--line:rgba(255,255,255,.1)}
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;background:transparent;font-family:system-ui,"Segoe UI",sans-serif;color:var(--text)}
body{overflow:hidden}button{font:inherit}#app,.hero{width:100%;height:100%}canvas{display:block;width:100%;height:100%}
header,.hero-bar,.grid,footer,.filters,.actions{display:none}
body.gallery{background:#0b1020;overflow:auto;height:auto;min-height:100vh;
 background-image:radial-gradient(ellipse at 8% 0%,rgba(86,225,255,.08),transparent 45%),radial-gradient(ellipse at 100% 50%,rgba(173,114,255,.08),transparent 45%)}
.gallery #app{max-width:1240px;height:auto;margin:auto;padding:30px}
.gallery header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:22px}
.kicker{color:var(--a);font-size:10px;letter-spacing:.2em;font-weight:700;margin:0 0 8px}
h1{font-size:30px;line-height:1.1;letter-spacing:-.04em;margin:0}header p{font-size:13px;color:var(--muted);margin:10px 0 0}
.options{display:flex;flex-direction:column;gap:8px}.demo{display:flex;align-items:center;gap:9px;padding:10px 14px;border:1px solid var(--line);border-radius:999px;font-size:12px;white-space:nowrap;cursor:pointer}
.demo input{accent-color:var(--a);width:15px;height:15px}
.gallery .hero{height:auto;overflow:hidden;border:1px solid var(--line);border-radius:18px;background:rgba(5,10,21,.7)}
.gallery .hero-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 20px;border-bottom:1px solid rgba(255,255,255,.05)}
.hero-title{font-size:14px;font-weight:600}#signal{font-size:11px;color:var(--muted)}#signal.demo-on{color:#ffcb78}
.gallery #main{height:220px}
.gallery .actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;position:sticky;top:10px;z-index:2;margin-top:14px;padding:12px;background:#101a2b;border:1px solid var(--line);border-radius:12px}
.actions button,.actions a{border:1px solid var(--line);border-radius:8px;padding:8px 12px;background:transparent;color:var(--text);font:inherit;font-size:12px;cursor:pointer;text-decoration:none}.actions #use-preset{background:var(--a);color:#081018;border-color:var(--a);font-weight:600}.actions button:disabled{opacity:.6;cursor:default}.actions output{font-size:12px;color:var(--muted)}.actions input{min-width:180px;flex:1;background:#081018;border:1px solid var(--line);border-radius:6px;color:var(--muted);padding:7px;font-size:11px}
.gallery .filters{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}.filters button{padding:8px 13px;border:1px solid var(--line);border-radius:999px;background:transparent;color:var(--muted);font-size:12px;cursor:pointer}.filters button[aria-pressed=true]{background:rgba(86,225,255,.1);border-color:var(--a);color:var(--text)}
.gallery .grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:18px}
.preset{text-align:left;color:var(--text);border:1px solid var(--line);border-radius:14px;padding:0;overflow:hidden;
 background:rgba(10,17,32,.75);cursor:pointer;transition:border-color .2s,background .2s;min-width:0}
.preset:hover{border-color:rgba(86,225,255,.45);background:#121e33}.preset:focus-visible{outline:2px solid var(--a);outline-offset:3px}
.preset[aria-pressed=true]{border-color:var(--a);background:rgba(86,225,255,.04)}
.preset canvas{height:126px}.preset-label{display:flex;justify-content:space-between;align-items:center;gap:6px;padding:12px 14px;font-size:12px;font-weight:600}
.choice{color:var(--a);font-size:10px;visibility:hidden}.preset[aria-pressed=true] .choice{visibility:visible}
.gallery footer{display:block;color:var(--muted);font-size:12px;line-height:1.7;margin-top:18px}footer strong{color:var(--text);font-weight:500}
@media(max-width:800px){.gallery .grid{grid-template-columns:repeat(2,minmax(0,1fr))}.gallery #app{padding:20px}.gallery header{align-items:flex-start}h1{font-size:26px}}
@media(max-width:440px){.gallery header{flex-direction:column}.gallery .hero-bar{align-items:flex-start;flex-direction:column}.gallery #main{height:180px}}
</style></head>
<body><main id="app">
<header><div><p class="kicker">AUDIO / OBS</p><h1>Encuentra tu ritmo.</h1><p>38 formas de ver la misma música. Elige la que más te guste.</p></div>
<div class="options"><label class="demo"><input id="demo" type="checkbox">Comparar con demostración</label><label class="demo"><input id="link-colors" type="checkbox">Colores del overlay MusicBee</label></div></header>
<section class="hero"><div class="hero-bar"><span class="hero-title" id="selected">Ondas suaves</span><span id="signal">Esperando audio de MusicBee</span></div><canvas id="main" aria-label="Visualizador de audio"></canvas></section>
<div class="actions"><button id="use-preset" type="button">Usar en OBS</button><button id="copy-url" type="button">Copiar URL</button><a id="open-overlay" target="_blank" rel="noopener">Abrir overlay</a><output id="preset-status" role="status"></output><input id="preset-url" readonly aria-label="URL del preset para la fuente de navegador"></div>
<nav class="filters" id="filters" aria-label="Filtrar estilos"></nav>
<section class="grid" id="grid" aria-label="Estilos de visualizador"></section>
<footer>Pulsa <strong>Usar en OBS</strong> para aplicar <strong id="choice-label">Ondas suaves</strong> a la fuente principal. Puedes copiar su URL para usarlo en otra fuente. Configura también el vídeo de OBS a <strong>60 FPS</strong>. La demostración solo se muestra en esta galería.</footer>
</main>
<script>
const STYLES={"bars":"Barras clásicas","mirror":"Barras espejo","ribbon":"Ondas suaves","scope":"Osciloscopio","ring":"Espectro circular","orbit":"Anillo fluido","dots":"Barras LED","particles":"Partículas","bars_peaks":"Barras con picos","bars_horizontal":"Barras horizontales","bars_split":"Barras enfrentadas","bars_edges":"Barras desde los bordes","bars_center":"Barras hacia el centro","bars_rounded":"Píldoras","bars_thin":"Líneas finas","bars_steps":"Escalones","ribbon_outline":"Contorno de onda","ribbon_layers":"Ondas en capas","ribbon_single":"Montaña de frecuencias","ribbon_dual":"Ondas cruzadas","ribbon_tunnel":"Túnel de ondas","scope_double":"Osciloscopio doble","scope_fill":"Osciloscopio relleno","scope_dots":"Osciloscopio punteado","scope_trail":"Estela de osciloscopio","scope_xy":"Órbita de la señal","ring_inward":"Anillo hacia dentro","ring_double":"Doble anillo","ring_dots":"Círculo de puntos","ring_polygon":"Polígono reactivo","ring_spiral":"Espiral espectral","ring_flower":"Flor sonora","ring_sun":"Rayos de sonido","orbit_radar":"Radar sonoro","orbit_ripples":"Ecos circulares","grid_heat":"Mosaico espectral","particles_fountain":"Fuente de partículas","particles_constellation":"Constelación"};
const gallery=location.pathname.replace(/\/$/,'')==='/compare';
if(gallery)document.body.classList.add('gallery');
const queryStyle=new URLSearchParams(location.search).get('style');
let selection=STYLES[queryStyle]?queryStyle:'ribbon', clicked=false;
let config={style:'ribbon',color_a:'#56e1ff',color_b:'#ad72ff',background_color:'#081018',background:false,glow:true,intensity:1,smoothing:.2,attack_ms:18,release_ms:140,thickness:3,density:64};
let packet={bands:new Array(128).fill(0),wave:new Array(1024).fill(0),level:0,ready:false,source:'',error:null};
let received=0, demo=false;
let overlayPalette=null,paletteKey='',palettePort=null,themeBusy=false;
const clamp=(v,a=0,b=1)=>Math.min(b,Math.max(a,Number.isFinite(v)?v:0));
function resample(values,n){if(values.length===n)return values;const last=values.length-1;return Array.from({length:n},(_,i)=>{const p=i*last/(n-1),j=Math.floor(p);return (values[j]||0)*(1-p+j)+(values[Math.min(last,j+1)]||0)*(p-j)});}
function curve(g,points){g.moveTo(...points[0]);for(let i=1;i<points.length-1;i++){g.quadraticCurveTo(...points[i],(points[i][0]+points[i+1][0])/2,(points[i][1]+points[i+1][1])/2);}g.lineTo(...points[points.length-1]);}
function rounded(g,x,y,w,h,r,fill=true){r=Math.min(r,w/2,h/2);if(fill)g.beginPath();g.moveTo(x+r,y);g.arcTo(x+w,y,x+w,y+h,r);g.arcTo(x+w,y+h,x,y+h,r);g.arcTo(x,y+h,x,y,r);g.arcTo(x,y,x+w,y,r);g.closePath();if(fill)g.fill();}
class Visualizer{
 constructor(canvas,style){this.canvas=canvas;this.surface=document.createElement('canvas');this.g=this.surface.getContext('2d');this.output=canvas.getContext('2d');this.history=document.createElement('canvas');this.historyContext=this.history.getContext('2d');this.style=style;this.lastStyle=style;this.waveSignal=[];this.waveKey=null;this.v=new Array(128).fill(0);this.wave=new Array(1024).fill(0);this.waveReference=.48;this.peaks=new Array(128).fill(0);this.trails=[];this.ripples=[];this.lastBass=0;this.visible=!gallery;this.level=0;this.phase=0;this.w=0;this.h=0;this.key='';this.observer=new ResizeObserver(()=>this.resize());this.observer.observe(canvas);this.resize();if(gallery){this.visibility=new IntersectionObserver(entries=>{this.visible=entries[0].isIntersecting;},{rootMargin:"100px"});this.visibility.observe(canvas);}}
 resize(){const r=this.canvas.getBoundingClientRect();this.w=r.width;this.h=r.height;const d=Math.min(window.devicePixelRatio||1,gallery?1.5:2);this.canvas.width=this.surface.width=this.history.width=Math.round(this.w*d);this.canvas.height=this.surface.height=this.history.height=Math.round(this.h*d);this.g.setTransform(d,0,0,d,0,0);this.output.setTransform(d,0,0,d,0,0);this.key='';}
 draw(data,dt,c){
  const {g,w,h}=this;if(!w||!h||!this.visible)return;
  const live=data.ready, bands=live?resample(data.bands,128):null;
  const scope=this.style==='scope'||this.style.startsWith('scope_');
  if(this.lastStyle!==this.style){this.historyContext.clearRect(0,0,this.history.width,this.history.height);this.ripples=[];this.lastStyle=this.style;}
  const smooth=clamp(c.smoothing), attack=c.attack_ms>0?1-Math.exp(-dt*1000/c.attack_ms):1,release=c.release_ms>0?1-Math.exp(-dt*1000/c.release_ms):1;
  for(let i=0;i<128;i++){const t=bands?clamp(bands[i]):0;this.v[i]+=(t-this.v[i])*(t>this.v[i]?attack:release);this.peaks[i]=Math.max(this.v[i],this.peaks[i]*Math.exp(-dt*2));}
  const rawLevel=clamp(live?data.level:0);this.level+=(rawLevel-this.level)*(rawLevel>this.level?attack:release);
  if(scope&&live&&rawLevel>.00001){this.wave=resample(data.wave,1024);this.waveReference=rawLevel;}
  const waveScale=Math.min(2,this.level/Math.max(.00001,this.waveReference));
  if(scope&&(this.waveKey!==this.wave||this.waveSmoothing!==smooth)){this.waveSignal=this.wave.map((v,i)=>v*(1-smooth)+smooth*((this.wave[Math.max(0,i-1)]+2*v+this.wave[Math.min(1023,i+1)])/4));this.waveKey=this.wave;this.waveSmoothing=smooth;}
  const waveSignal=this.waveSignal;
  this.phase+=dt*this.level*.6;
  g.clearRect(0,0,w,h);g.globalAlpha=1;g.lineCap='round';g.lineJoin='round';
  const key=`${w},${h},${c.color_a},${c.color_b}`;if(key!==this.key){this.gradient=g.createLinearGradient(w*.08,h,w*.92,0);this.gradient.addColorStop(0,c.color_a);this.gradient.addColorStop(1,c.color_b);this.key=key;}
  g.fillStyle=g.strokeStyle=this.gradient;g.shadowColor=c.color_a;g.shadowBlur=0;g.lineWidth=c.thickness;
  const n=Math.round(clamp(c.density,16,256)), values=resample(this.v.map((v,i)=>v*(1-smooth)+smooth*(this.v[Math.max(0,i-1)]+2*v+this.v[Math.min(127,i+1)])/4),n).map(v=>clamp(v*c.intensity)), pad=w*.07, width=w-2*pad;
  const mirrored=()=>{const half=resample(values,Math.ceil(n/2));return half.slice().reverse().concat(half);};
  if(this.style==='bars'||this.style==='mirror'){
   const vals=this.style==='mirror'?mirrored():values;const step=width/vals.length,bw=Math.max(1,step*.68);
   g.beginPath();vals.forEach((v,i)=>{const size=Math.max(1.5,v*h*(this.style==='mirror'?.36:.72));const y=this.style==='mirror'?h/2-size:h*.86-size;rounded(g,pad+i*step,y,bw,this.style==='mirror'?2*size:size,Math.min(3,bw/2),false);});g.fill();
  }else if(this.style==='ribbon'){
   const vals=mirrored(),step=width/(vals.length-1),mid=h/2;
   const top=vals.map((v,i)=>[pad+i*step,mid-Math.pow(v,.9)*h*.35]);const bottom=vals.map((v,i)=>[pad+i*step,mid+Math.pow(v,.9)*h*.29]).reverse();
   g.beginPath();curve(g,top);g.lineTo(...bottom[0]);for(let i=1;i<bottom.length-1;i++)g.quadraticCurveTo(...bottom[i],(bottom[i][0]+bottom[i+1][0])/2,(bottom[i][1]+bottom[i+1][1])/2);g.lineTo(...bottom[bottom.length-1]);g.closePath();g.globalAlpha=.85;g.fill();g.globalAlpha=1;
   g.beginPath();curve(g,top);g.strokeStyle='rgba(255,255,255,.65)';g.lineWidth=Math.max(1,c.thickness*.4);g.stroke();
  }else if(this.style==='scope'){
   const pts=waveSignal.map((v,i)=>[pad+i*width/1023,h/2-clamp(v*3.5*c.intensity*waveScale,-1,1)*h*.38]);
   g.beginPath();g.moveTo(...pts[0]);for(let i=1;i<pts.length;i++)g.lineTo(...pts[i]);g.stroke();
  }else if(this.style==='ring'){
   const radius=Math.min(w,h)*.27,cx=w/2,cy=h/2;g.globalAlpha=.18;g.beginPath();g.arc(cx,cy,radius,0,Math.PI*2);g.stroke();g.globalAlpha=1;
   values.forEach((v,i)=>{const a=i/n*Math.PI*2-Math.PI/2,extent=radius+v*Math.min(w,h)*.18;g.lineWidth=Math.max(1,Math.min(c.thickness+1,radius*4/n));g.beginPath();g.moveTo(cx+Math.cos(a)*radius,cy+Math.sin(a)*radius);g.lineTo(cx+Math.cos(a)*extent,cy+Math.sin(a)*extent);g.stroke();});
  }else if(this.style==='orbit'){
   const size=Math.min(w,h),base=size*(.22+.025*this.level),cx=w/2,cy=h/2;
   for(let layer=2;layer>=0;layer--){const pts=values.map((v,i)=>{const a=i/n*Math.PI*2-Math.PI/2,rad=base+v*size*(.12+layer*.035);return[cx+Math.cos(a)*rad,cy+Math.sin(a)*rad];});pts.push(pts[0],pts[1]);g.beginPath();curve(g,pts);g.closePath();g.globalAlpha=layer===0?.9:.14;g.lineWidth=layer===0?c.thickness:c.thickness*2;g.stroke();}g.globalAlpha=1;
  }else if(this.style==='dots'){
   const rows=12,step=width/n,dy=h*.72/rows,r=Math.max(.8,Math.min(step*.27,dy*.32));
   // Agrupar los LED en dos caminos: apagados y encendidos, sin una sombra por LED.
   for(const active of [false,true]){g.beginPath();g.globalAlpha=active?1:.07;values.forEach((v,i)=>{const lit=Math.round(v*rows);for(let j=0;j<rows;j++){if((j<lit)!==active)continue;const x=pad+(i+.5)*step,y=h*.86-j*dy;g.moveTo(x+r,y);g.arc(x,y,r,0,Math.PI*2);}});g.fill();}g.globalAlpha=1;
  }else if(this.style==='particles'){
   const size=Math.min(w,h),cx=w/2,cy=h/2;
   for(let i=0;i<96;i++){const v=values[i%n];if(v<.01)continue;const angle=i*2.399963+this.phase*((i%2)?1:-1),rad=size*(.06+.3*Math.sqrt((i+.5)/96))*(.65+.55*v);
    const x=cx+Math.cos(angle)*rad*(w>h?Math.min(2,w/h):1),y=cy+Math.sin(angle)*rad;g.globalAlpha=clamp(v*.95);g.beginPath();g.arc(x,y,Math.max(.7,(1+i%4)*v*size*.012),0,Math.PI*2);g.fill();}g.globalAlpha=1;
  }else if(this.style.startsWith('bars_')){
   const step=width/n,bw=Math.max(1,step*.65),mid=h/2;
   if(this.style==='bars_horizontal'){
    const count=Math.min(n,24),vals=resample(values,count),dy=h*.8/count;
    vals.forEach((v,i)=>rounded(g,pad,h*.1+i*dy,Math.max(2,v*width),dy*.55,dy*.25));
   }else if(this.style==='bars_split'){
    const count=Math.min(n,32),vals=resample(values,count),dy=h*.8/count;
    vals.forEach((v,i)=>{const len=Math.max(1,v*width*.43),y=h*.1+i*dy;rounded(g,w/2-len-3,y,len,dy*.65,2);rounded(g,w/2+3,y,len,dy*.65,2);});
   }else if(this.style==='bars_center'){
    values.forEach((v,i)=>{const len=Math.max(2,v*h*.38),x=pad+i*step;rounded(g,x,mid-len,bw,len*2,bw/2);g.globalAlpha=.22;g.beginPath();g.arc(x+bw/2,mid,bw,0,Math.PI*2);g.fill();g.globalAlpha=1;});
   }else if(this.style==='bars_edges'){
    values.forEach((v,i)=>{const len=Math.max(1,v*h*.36),x=pad+i*step;rounded(g,x,h*.07,bw,len,2);rounded(g,x,h*.93-len,bw,len,2);});
   }else if(this.style==='bars_steps'){
    const rows=18,dy=h*.75/rows;
    g.beginPath();values.forEach((v,i)=>{for(let j=0;j<Math.round(v*rows);j++)g.rect(pad+i*step,h*.88-(j+1)*dy,bw,dy*.65);});g.fill();
   }else{
    const vals=this.style==='bars_rounded'?resample(values,Math.min(n,24)):values,dx=width/vals.length;
    const peakValues=this.style==='bars_peaks'?resample(this.peaks,vals.length):null;
    vals.forEach((v,i)=>{const len=Math.max(2,v*h*.72),barw=this.style==='bars_thin'?Math.min(2,dx*.3):dx*(this.style==='bars_rounded'?.78:.65);
     rounded(g,pad+i*dx,h*.86-len,barw,len,this.style==='bars_rounded'?barw/2:2);
     if(this.style==='bars_peaks'){const peak=peakValues[i];g.globalAlpha=.8;rounded(g,pad+i*dx,h*.86-clamp(peak*c.intensity)*h*.72-4,barw,2,1);g.globalAlpha=1;}
    });
   }
  }else if(this.style.startsWith('ribbon_')){
   const vals=this.style==='ribbon_single'?values:mirrored(),step=width/(vals.length-1),mid=h/2;
   if(this.style==='ribbon_single'){
    const pts=vals.map((v,i)=>[pad+i*step,h*.84-v*h*.65]);g.beginPath();curve(g,pts);g.lineTo(w-pad,h*.84);g.lineTo(pad,h*.84);g.closePath();g.globalAlpha=.75;g.fill();g.globalAlpha=1;g.beginPath();curve(g,pts);g.stroke();
   }else if(this.style==='ribbon_dual'){
    for(const sign of [-1,1]){const pts=vals.map((v,i)=>[pad+i*step,mid+sign*v*h*.3]);g.beginPath();curve(g,pts);g.stroke();}
    g.globalAlpha=.25;g.beginPath();curve(g,vals.map((v,i)=>[pad+i*step,mid+(v-.3)*h*.3]));g.stroke();g.globalAlpha=1;
   }else{
    const layers=this.style==='ribbon_outline'?1:this.style==='ribbon_tunnel'?7:4;
    for(let layer=layers-1;layer>=0;layer--){const factor=this.style==='ribbon_tunnel'?(layer+1)/layers:1-layer*.16,offset=this.style==='ribbon_layers'?layer*h*.05:0;
     const top=vals.map((v,i)=>[pad+i*step,mid-offset-v*h*.3*factor]);const bottom=vals.map((v,i)=>[pad+i*step,mid-offset+v*h*.27*factor]).reverse();
     g.beginPath();curve(g,top);g.lineTo(...bottom[0]);for(let i=1;i<bottom.length;i++)g.lineTo(...bottom[i]);g.closePath();g.globalAlpha=layer===0?1:.22;
     if(this.style==='ribbon_layers'){g.globalAlpha=.16+(.18*(layers-layer));g.fill();}g.stroke();
    }g.globalAlpha=1;
   }
  }else if(this.style.startsWith('scope_')){
   const amplitude=h*.34,pts=waveSignal.map((v,i)=>[pad+i*width/(waveSignal.length-1),h/2-clamp(v*3.5*c.intensity*waveScale,-1,1)*amplitude]);
   const line=points=>{g.beginPath();g.moveTo(...points[0]);for(let i=1;i<points.length;i++)g.lineTo(...points[i]);g.stroke();};
   if(this.style==='scope_fill'){g.beginPath();g.moveTo(...pts[0]);for(let i=1;i<pts.length;i++)g.lineTo(...pts[i]);g.lineTo(w-pad,h/2);g.lineTo(pad,h/2);g.closePath();g.globalAlpha=.35;g.fill();g.globalAlpha=1;line(pts);
   }else if(this.style==='scope_double'){for(const sign of [-1,1])line(pts.map(([x,y])=>[x,h/2+sign*(y-h/2)*.65+sign*h*.17]));
   }else if(this.style==='scope_dots'){for(let i=0;i<pts.length;i+=Math.max(1,Math.round(pts.length/n))){g.beginPath();g.arc(...pts[i],Math.max(1,c.thickness),0,Math.PI*2);g.fill();}
   }else if(this.style==='scope_trail'){
    // Persistencia en una textura: una onda por cuadro en vez de redibujar 12.
    g.globalAlpha=Math.exp(-dt/.075);g.drawImage(this.history,0,0,w,h);g.globalAlpha=1;line(pts);
    this.historyContext.clearRect(0,0,this.history.width,this.history.height);this.historyContext.drawImage(this.surface,0,0);
   }else if(this.style==='scope_xy'){const size=Math.min(w,h),rad=size*.27;g.beginPath();for(let i=0;i<waveSignal.length;i++){const a=i/waveSignal.length*Math.PI*2,rr=rad+waveSignal[i]*3*c.intensity*waveScale*size*.12;const x=w/2+Math.cos(a)*rr,y=h/2+Math.sin(a)*rr;i?g.lineTo(x,y):g.moveTo(x,y);}g.closePath();g.stroke();}
  }else if(this.style.startsWith('ring_')){
   const size=Math.min(w,h),rad=size*.27,cx=w/2,cy=h/2;
   if(this.style==='ring_dots'){
    values.forEach((v,i)=>{const a=i/n*Math.PI*2-Math.PI/2,r=rad+v*size*.09;g.globalAlpha=.2+.8*v;g.beginPath();g.arc(cx+Math.cos(a)*r,cy+Math.sin(a)*r,Math.max(1,c.thickness+v*5),0,Math.PI*2);g.fill();});g.globalAlpha=1;
   }else if(this.style==='ring_polygon'){
    const vals=resample(values,8),pts=vals.map((v,i)=>{const a=i/8*Math.PI*2-Math.PI/2,r=rad+v*size*.13;return[cx+Math.cos(a)*r,cy+Math.sin(a)*r];});g.beginPath();g.moveTo(...pts[0]);for(let i=1;i<pts.length;i++)g.lineTo(...pts[i]);g.closePath();g.globalAlpha=.18;g.fill();g.globalAlpha=1;g.stroke();
   }else if(this.style==='ring_spiral'){
    const vals=resample(values,n*3);g.beginPath();vals.forEach((v,i)=>{const a=i/vals.length*Math.PI*6+this.phase,r=size*(.04+i/vals.length*.31+v*.05),x=cx+Math.cos(a)*r,y=cy+Math.sin(a)*r;i?g.lineTo(x,y):g.moveTo(x,y);});g.stroke();
   }else if(this.style==='ring_flower'){
    const pts=values.map((v,i)=>{const a=i/n*Math.PI*2,r=rad+v*size*.07+Math.sin(a*6)*this.level*size*.16;return[cx+Math.cos(a)*r,cy+Math.sin(a)*r];});pts.push(pts[0],pts[1]);g.beginPath();curve(g,pts);g.closePath();g.globalAlpha=.14;g.fill();g.globalAlpha=1;g.stroke();
   }else{
    values.forEach((v,i)=>{const a=i/n*Math.PI*2-Math.PI/2,inner=this.style==='ring_inward'?rad-v*size*.19:rad,outer=this.style==='ring_inward'?rad:rad+v*size*.17;
     g.beginPath();g.moveTo(cx+Math.cos(a)*inner,cy+Math.sin(a)*inner);g.lineTo(cx+Math.cos(a)*outer,cy+Math.sin(a)*outer);g.stroke();
     if(this.style==='ring_double'){g.globalAlpha=.6;g.beginPath();g.moveTo(cx+Math.cos(a)*rad*.65,cy+Math.sin(a)*rad*.65);g.lineTo(cx+Math.cos(a)*(rad*.65-v*size*.13),cy+Math.sin(a)*(rad*.65-v*size*.13));g.stroke();g.globalAlpha=1;}
     if(this.style==='ring_sun'){g.beginPath();g.moveTo(cx+Math.cos(a)*rad*.25,cy+Math.sin(a)*rad*.25);g.lineTo(cx+Math.cos(a)*outer,cy+Math.sin(a)*outer);g.globalAlpha=.2+.6*v;g.stroke();g.globalAlpha=1;}
    });
    g.globalAlpha=.3;g.beginPath();g.arc(cx,cy,rad,0,Math.PI*2);g.stroke();g.globalAlpha=1;
   }
  }else if(this.style==='orbit_radar'){
   const size=Math.min(w,h),rad=size*.35;g.globalAlpha=.1;for(let i=1;i<=3;i++){g.beginPath();g.arc(w/2,h/2,rad*i/3,0,Math.PI*2);g.stroke();}g.globalAlpha=1;
   resample(values,24).forEach((v,i)=>{const a=i/24*Math.PI*2+this.phase;g.globalAlpha=.2+.8*v;g.beginPath();g.moveTo(w/2,h/2);g.lineTo(w/2+Math.cos(a)*rad*v,h/2+Math.sin(a)*rad*v);g.stroke();});g.globalAlpha=1;
  }else if(this.style==='orbit_ripples'){
   const bass=this.v.slice(0,12).reduce((a,b)=>a+b,0)/12;if(bass>this.lastBass+.025&&bass>.15)this.ripples.push({radius:.05,alpha:bass});this.lastBass=bass;
   if(this.ripples.length>12)this.ripples.shift();for(const ripple of this.ripples){ripple.radius+=dt*.22;ripple.alpha-=dt*.7;g.globalAlpha=Math.max(0,ripple.alpha);g.beginPath();g.arc(w/2,h/2,Math.min(w,h)*ripple.radius,0,Math.PI*2);g.stroke();}
   this.ripples=this.ripples.filter(r=>r.alpha>0);g.globalAlpha=.3+this.level;g.beginPath();g.arc(w/2,h/2,Math.min(w,h)*(.13+.06*this.level),0,Math.PI*2);g.stroke();g.globalAlpha=1;
  }else if(this.style==='grid_heat'){
   const cols=16,rows=8,dx=width/cols,dy=h*.76/rows;
   resample(values,cols*rows).forEach((v,i)=>{g.globalAlpha=.04+.9*v;rounded(g,pad+(i%cols)*dx,h*.12+Math.floor(i/cols)*dy,dx*.8,dy*.8,Math.min(dx,dy)*.12);});g.globalAlpha=1;
  }else if(this.style==='particles_fountain'){
   const vals=resample(values,64);
   vals.forEach((v,i)=>{if(v<.02)return;const travel=(this.phase*(.7+i%5*.1)+i*.618)%1,x=pad+width*i/63+Math.sin(i*1.8)*width*.03*travel,y=h*.86-travel*h*.7*v;g.globalAlpha=v*(1-travel*.6);g.beginPath();g.arc(x,y,Math.max(1,c.thickness*.5+v*4),0,Math.PI*2);g.fill();});g.globalAlpha=1;
  }else if(this.style==='particles_constellation'){
   const vals=resample(values,48),points=vals.map((v,i)=>[pad+((i*.6180339)%1)*width,h*.1+((i*.4142135)%1)*h*.8,v]);
   for(let i=0;i<points.length;i++){const[x,y,v]=points[i];if(v<.025)continue;g.globalAlpha=v;g.beginPath();g.arc(x,y,Math.max(1,c.thickness*.6+v*3),0,Math.PI*2);g.fill();for(let j=i+1;j<points.length;j++){const[xx,yy,vv]=points[j];if(vv>.1&&Math.hypot(xx-x,yy-y)<Math.min(w,h)*.25){g.globalAlpha=Math.min(v,vv)*.22;g.lineWidth=1;g.beginPath();g.moveTo(x,y);g.lineTo(xx,yy);g.stroke();}}}g.globalAlpha=1;
  }
  g.shadowBlur=0;g.globalAlpha=1;
  // Componer el brillo una sola vez para toda la geometría del cuadro.
  const out=this.output;out.clearRect(0,0,w,h);out.shadowColor=c.color_a;out.shadowBlur=c.glow?Math.min(12,h*.06):0;out.drawImage(this.surface,0,0,w,h);out.shadowBlur=0;
 }
}
const main=new Visualizer(document.getElementById('main'),selection),renderers=[main];
function presetURL(){return new URL('/?style='+encodeURIComponent(selection),location.href).href;}
function updateChoice(){
 if(!gallery)return;
 const active=selection===config.style,button=document.getElementById('use-preset');
 button.disabled=active;button.textContent=active?'En uso en OBS':'Usar en OBS';
 document.getElementById('preset-url').value=presetURL();document.getElementById('open-overlay').href=presetURL();
}
function select(style){selection=style;main.style=style;document.getElementById('selected').textContent=STYLES[style];document.getElementById('choice-label').textContent=STYLES[style];document.querySelectorAll('.preset').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.style===style)));document.getElementById('preset-status').textContent='';updateChoice();}
if(gallery){
 const group=id=>id.startsWith('particles')?'Partículas':id.startsWith('ring')||id.startsWith('orbit')?'Circulares':id.startsWith('scope')||id.startsWith('ribbon')?'Ondas':'Barras';
 for(const category of ['Todos','Barras','Ondas','Circulares','Partículas']){const button=document.createElement('button');button.type='button';button.textContent=category;button.setAttribute('aria-pressed',String(category==='Todos'));button.addEventListener('click',()=>{document.querySelectorAll('#filters button').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));document.querySelectorAll('.preset').forEach(el=>{el.hidden=category!=='Todos'&&group(el.dataset.style)!==category;});});document.getElementById('filters').append(button);}
 for(const [id,label]of Object.entries(STYLES)){const button=document.createElement('button');button.type='button';button.className='preset';button.dataset.style=id;button.setAttribute('aria-label',label);const canvas=document.createElement('canvas');canvas.setAttribute('aria-hidden','true');const caption=document.createElement('div');caption.className='preset-label';const name=document.createElement('span');name.textContent=label;const check=document.createElement('span');check.className='choice';check.textContent='Elegido';caption.append(name,check);button.append(canvas,caption);document.getElementById('grid').append(button);renderers.push(new Visualizer(canvas,id));button.addEventListener('click',()=>{clicked=true;select(id);});}
 document.getElementById('demo').addEventListener('change',e=>{demo=e.target.checked;});
 document.getElementById('use-preset').addEventListener('click',async()=>{
  const style=selection,button=document.getElementById('use-preset'),status=document.getElementById('preset-status');button.disabled=true;
  try{const response=await fetch('/preset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({style})});if(!response.ok)throw Error('No disponible');config.style=style;status.textContent=STYLES[style]+' aplicado a OBS';}
  catch(e){status.textContent='No se pudo aplicar. Vuelve a intentarlo.';}
  finally{updateChoice();}
 });
 document.getElementById('link-colors').addEventListener('change',async event=>{
  const enabled=event.target.checked;event.target.disabled=true;
  try{const response=await fetch('/colors',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});if(!response.ok)throw Error('No disponible');}
  catch(e){event.target.checked=!!config.link_colors;document.getElementById('preset-status').textContent='No se pudo cambiar el color. Vuelve a intentarlo.';}
  finally{event.target.disabled=false;}
 });
 document.getElementById('copy-url').addEventListener('click',async()=>{
  const field=document.getElementById('preset-url'),status=document.getElementById('preset-status');
  try{await navigator.clipboard.writeText(presetURL());status.textContent='URL copiada. Pégala en tu fuente de navegador.';}
  catch(e){field.focus();field.select();status.textContent='Copia la URL seleccionada y pégala en tu fuente de navegador.';}
 });select(selection);
}
function acceptState(s){const linkChanged=config.link_colors!==s.config.link_colors||config.musicbee_port!==s.config.musicbee_port;const colorChanged=config.color_a!==s.config.color_a||config.color_b!==s.config.color_b;packet=s;config=s.config;received=performance.now();if(colorChanged||linkChanged)refreshColors();if(linkChanged){if(!config.link_colors||palettePort!==config.musicbee_port){overlayPalette=null;paletteKey='';}if(gallery)document.getElementById('link-colors').checked=!!config.link_colors;syncTheme();}
 if(!gallery){main.style=STYLES[queryStyle]?queryStyle:config.style;document.body.style.background=config.background?effectiveConfig().background_color:'transparent';}
 else{if(!clicked&&!STYLES[queryStyle]&&selection!==config.style)select(config.style);updateChoice();}
}
async function poll(){try{const response=await fetch('/state.json',{cache:'no-store'});if(!response.ok)throw Error('No disponible');acceptState(await response.json());}catch(e){packet.ready=false;}finally{setTimeout(poll,8);}}
function connect(){if(window.EventSource){const stream=new EventSource('/events');stream.onmessage=e=>{try{acceptState(JSON.parse(e.data));}catch(e){packet.ready=false;}};stream.onerror=()=>{packet.ready=false;};}else poll();}
function demonstration(t){return{ready:true,bands:Array.from({length:128},(_,i)=>clamp((.3+.27*Math.sin(t*2.1-i*.32)+.15*Math.sin(t*.8+i*.65))*(1-i*.008))),wave:Array.from({length:1024},(_,i)=>.2*Math.sin(i*.19+t)+.055*Math.sin(i*.57-t)),level:.4+.08*Math.sin(t*2)};}
function rgb2hsl(r,g,b){
 r/=255;g/=255;b/=255;const mx=Math.max(r,g,b),mn=Math.min(r,g,b),d=mx-mn,l=(mx+mn)/2;let h=0,s=0;
 if(d){s=d/(1-Math.abs(2*l-1));if(mx===r)h=((g-b)/d)%6;else if(mx===g)h=(b-r)/d+2;else h=(r-g)/d+4;h*=60;if(h<0)h+=360;}
 return[h,s,l];
}
function coverPalette(image){
 const cv=document.createElement('canvas');cv.width=cv.height=32;const ctx=cv.getContext('2d');ctx.drawImage(image,0,0,32,32);
 const pixels=ctx.getImageData(0,0,32,32).data,bins=new Array(36).fill(0),sat=new Array(36).fill(0);
 for(let i=0;i<pixels.length;i+=4){const[h,s,l]=rgb2hsl(pixels[i],pixels[i+1],pixels[i+2]);if(l<.12||l>.9)continue;const weight=s*s*(1-Math.abs(l-.5)),bin=Math.floor(h/10)%36;bins[bin]+=weight;sat[bin]+=weight*s;}
 let best=0;for(let i=1;i<36;i++)if(bins[i]>bins[best])best=i;
 return bins[best]>.5?palette(best*10+5,sat[best]/bins[best]):palette(255,.35);
}
function palette(h,s){s=Math.min(1,Math.max(.55,s));return{color_a:`hsl(${h} ${s*100}% 46%)`,color_b:`hsl(${(h+38)%360} ${Math.min(100,s*100+10)}% 62%)`,background_color:`hsl(${h} 32% 9%)`};}
function effectiveConfig(){return config.link_colors&&overlayPalette&&palettePort===config.musicbee_port?{...config,...overlayPalette}:config;}
function refreshColors(){const c=effectiveConfig();document.documentElement.style.setProperty('--a',c.color_a);document.documentElement.style.setProperty('--b',c.color_b);}
async function syncTheme(){
 if(!config.link_colors||themeBusy)return;themeBusy=true;const port=config.musicbee_port;
 try{
  const response=await fetch('/musicbee-theme.json',{cache:'no-store'});if(!response.ok)throw Error('Sin overlay');const info=await response.json(),key=port+':'+info.rev+':'+info.has_cover;
  if(paletteKey===key&&overlayPalette&&palettePort===port)return;
  let colors=palette(200,.55);
  if(info.has_cover){const image=new Image();image.src='/musicbee-cover?rev='+encodeURIComponent(info.rev);await image.decode();colors=coverPalette(image);}
  if(!config.link_colors||config.musicbee_port!==port)return;
  overlayPalette=colors;paletteKey=key;palettePort=port;refreshColors();if(!gallery&&config.background)document.body.style.background=colors.background_color;
 }catch(e){if(config.musicbee_port===port){overlayPalette=null;paletteKey='';refreshColors();}}
 finally{themeBusy=false;}
}
setInterval(syncTheme,1000);
const FRAME_MS=1000/60;
let previous=performance.now(),nextDraw=0;
function frame(now){
 requestAnimationFrame(frame);
 if(now+1<nextDraw)return;
 nextDraw=nextDraw?nextDraw+FRAME_MS:now+FRAME_MS;if(nextDraw<now)nextDraw=now+FRAME_MS;
 const dt=Math.min(.1,(now-previous)/1000);previous=now;
 const data=gallery&&demo?demonstration(now/1000):{...packet,ready:packet.ready&&now-received<700};
 const drawConfig=effectiveConfig();main.draw(data,dt,drawConfig);
 if(gallery){
  // Las miniaturas visibles también se actualizan a 60 FPS, sin brillo costoso.
  const previewConfig={...drawConfig,glow:false,density:Math.min(64,config.density)};
  for(let i=1;i<renderers.length;i++)renderers[i].draw(data,dt,previewConfig);
  const signal=document.getElementById('signal');const label=demo?'Demostración · sin audio real':data.ready?`Audio de OBS · ${data.source||'MusicBee'}`:'Esperando audio de MusicBee';
  if(signal.textContent!==label){signal.textContent=label;signal.classList.toggle('demo-on',demo);}
 }
}
connect();requestAnimationFrame(frame);
</script></body></html>
"""


def _css_color(value):
    return '#{:02x}{:02x}{:02x}'.format(value & 255, (value >> 8) & 255, (value >> 16) & 255)


def _set_color_link(enabled):
    global _audio_seq, _pending_color_link
    if not isinstance(enabled, bool):
        raise ValueError('Opción de color no válida.')
    with _audio_updated:
        _cfg['link_colors'] = enabled
        _pending_color_link = enabled
        _audio_seq += 1
        _audio_updated.notify_all()


def _musicbee_request(path, limit):
    with _lock:
        port = _cfg['musicbee_port']
    if port == _httpd_port:
        raise RuntimeError('El puerto de MusicBee debe ser distinto del puerto de visualizadores.')
    with _local_http.open(f'http://127.0.0.1:{port}{path}', timeout=.75) as response:
        body = response.read(limit + 1)
        mime = response.headers.get_content_type()
        revision = response.headers.get('X-Cover-Revision')
    if len(body) > limit:
        raise RuntimeError('La respuesta de MusicBee es demasiado grande.')
    return port, body, mime, revision


def _musicbee_cover(revision):
    global _cover_cache
    with _lock:
        port, cached = _cfg['musicbee_port'], _cover_cache
    if cached and cached[:2] == (port, revision):
        return cached[2:]
    source_port, body, mime, actual_revision = _musicbee_request('/cover', 16 * 1024 * 1024)
    if not mime.startswith('image/') or (actual_revision is not None and str(revision) != actual_revision):
        raise RuntimeError('La portada ha cambiado. Vuelve a consultar sus colores.')
    with _lock:
        _cover_cache = (source_port, revision, body, mime)
    return body, mime


def _choose_preset(style):
    global _audio_seq, _pending_style
    if not isinstance(style, str) or style not in PRESETS:
        raise ValueError('Preset desconocido.')
    with _audio_updated:
        _cfg['style'] = style
        _pending_style = style
        _audio_seq += 1
        _audio_updated.notify_all()


def _snapshot():
    with _lock:
        config = {key: _cfg[key] for key in ('style', 'background', 'glow', 'intensity', 'smoothing', 'attack_ms', 'release_ms', 'thickness', 'density', 'link_colors', 'musicbee_port')}
        config.update({key: _css_color(_cfg[key]) for key in ('color_a', 'color_b', 'background_color')})
        ready = bool(_audio_ok and _cfg['visualizer'] and time.monotonic() - _audio['stamp'] < .3)
        return {
            'ready': ready, 'source': _audio_source_name, 'error': _audio_error,
            'bands': list(_audio['bands']) if ready else [0.0] * BANDS,
            'wave': list(_audio['wave']) if ready else [0.0] * WAVE_POINTS,
            'level': _audio['level'] if ready else 0.0, 'config': config, 'analysis': dict(_audio.get('analysis', {})),
        }


def _stream_state(handler, snapshot):
    """SSE: publicar cambios sin encadenar sondeos HTTP ni acumular cuadros antiguos."""
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    handler.end_headers()
    stopped = getattr(handler.server, 'stream_stop', _stop)
    sequence, next_frame = -1, 0.0
    try:
        while not _stop.is_set() and not stopped.is_set():
            with _audio_updated:
                _audio_updated.wait_for(lambda: _audio_seq != sequence or _stop.is_set() or stopped.is_set(), timeout=.2)
                sequence = _audio_seq
            if _stop.is_set() or stopped.is_set():
                break
            delay = next_frame - time.monotonic()
            if delay > 0 and _stop.wait(delay):
                break
            payload = json.dumps(snapshot(), allow_nan=False, separators=(',', ':')).encode('utf-8')
            handler.wfile.write(b'data: ' + payload + b'\n\n')
            handler.wfile.flush()
            next_frame = time.monotonic() + 1 / 60
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


class _Handler(BaseHTTPRequestHandler):
    disable_nagle_algorithm = True

    def log_message(self, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ('/preset', '/colors'):
            self._send_json(404, {'error': 'No encontrado.'})
            return
        if self.headers.get_content_type() != 'application/json':
            self._send_json(415, {'error': 'Se requiere JSON.'})
            return
        origin = self.headers.get('Origin')
        if origin:
            try:
                parsed = urlsplit(origin)
                allowed = (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')
                           and parsed.port == self.server.server_port)
            except ValueError:
                allowed = False
            if not allowed:
                self._send_json(403, {'error': 'Origen no permitido.'})
                return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024:
                raise ValueError('Petición no válida.')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Petición no válida.')
            if path == '/preset':
                _choose_preset(payload.get('style'))
            else:
                _set_color_link(payload.get('enabled'))
        except (ValueError, TypeError):
            self._send_json(400, {'error': 'Preset o petición no válido.'})
            return
        self._send_json(200, {'style': payload['style']} if path == '/preset' else {'enabled': payload['enabled']})

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip('/') or '/'
        if path == '/musicbee-theme.json':
            try:
                _, raw, _, _ = _musicbee_request('/now.json', 65536)
                info = json.loads(raw)
                self._send_json(200, {'rev': int(info['rev']), 'has_cover': info.get('has_cover') is True})
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                self._send_json(503, {'error': 'Overlay de MusicBee no disponible.'})
            return
        if path == '/musicbee-cover':
            try:
                params = parse_qs(urlsplit(self.path).query)
                revision = int(params.get('rev', ['-1'])[0])
                if revision < 0:
                    raise ValueError('Revisión de portada no válida.')
                body, mime = _musicbee_cover(revision)
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ValueError, RuntimeError):
                self._send_json(503, {'error': 'Portada no disponible.'})
            return
        if path == '/events':
            _stream_state(self, _snapshot)
            return
        if path in ('/', '/overlay', '/compare'):
            code, mime, body = 200, 'text/html; charset=utf-8', OVERLAY_HTML.encode('utf-8')
        elif path == '/state.json':
            code, mime, body = 200, 'application/json', json.dumps(_snapshot(), allow_nan=False).encode('utf-8')
        else:
            code, mime, body = 404, 'text/plain; charset=utf-8', b'No encontrado'
        try:
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _stop_server():
    global _httpd, _httpd_port
    if _httpd is not None:
        _httpd.stream_stop.set()
        with _audio_updated:
            _audio_updated.notify_all()
        _httpd.shutdown()
        _httpd.server_close()
    _httpd, _httpd_port = None, None


def _start_server(port):
    global _httpd, _httpd_port
    _stop_server()
    try:
        _httpd = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
        _httpd.stream_stop = threading.Event()
        _httpd_port = _httpd.server_port
        threading.Thread(target=_httpd.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
    except OSError as e:
        _httpd = None
        obs.script_log(obs.LOG_WARNING, f'OBS Visualizers: no se pudo abrir el puerto {port}: {e}')


def script_description():
    return (
        '<b>OBS Visualizers</b><br>38 visualizadores para el audio de MusicBee. '
        'Por defecto comparten el audio de musicbee_nowplaying.py (puerto 8765). '
        'Carga ambos scripts para usar ese modo. Animación a 60 FPS.<br>'
        'Fuente de navegador: <code>http://localhost:8766/</code>, 900×300, '
        'o 600×600 para los estilos circulares. '
        'Compara y aplica los 38 presets en <code>http://localhost:8766/compare</code> '
        'con Usar en OBS. Configura Ajustes → Vídeo de OBS a 60 FPS.<br>'
        'También puedes elegir una fuente directa de OBS. Ese modo requiere numpy '
        'en el Python de OBS. No utiliza VB-CABLE ni cambia el volumen audible.'
    )


def script_defaults(settings):
    for key, value in _cfg.items():
        if isinstance(value, bool):
            obs.obs_data_set_default_bool(settings, key, value)
        elif isinstance(value, float):
            obs.obs_data_set_default_double(settings, key, value)
        elif isinstance(value, int):
            obs.obs_data_set_default_int(settings, key, value)
        else:
            obs.obs_data_set_default_string(settings, key, value)


def _fill_audio_sources(prop):
    obs.obs_property_list_clear(prop)
    obs.obs_property_list_add_string(prop, '(selecciona una fuente)', '')
    sources = obs.obs_enum_sources()
    if sources:
        try:
            for source in sources:
                if obs.obs_source_get_output_flags(source) & obs.OBS_SOURCE_AUDIO:
                    name = obs.obs_source_get_name(source)
                    obs.obs_property_list_add_string(prop, name, name)
        finally:
            obs.source_list_release(sources)


def _refresh_audio_sources(props, prop):
    _fill_audio_sources(obs.obs_properties_get(props, 'audio_source'))
    return True


def _mode_visibility(props, mode, link_colors=None):
    for name in ('audio_source', 'refresh_audio_sources', 'normalize', 'analysis_size'):
        obs.obs_property_set_visible(obs.obs_properties_get(props, name), mode == 'obs')
    if link_colors is None:
        link_colors = _cfg['link_colors']
    obs.obs_property_set_visible(obs.obs_properties_get(props, 'musicbee_port'), mode == 'musicbee' or link_colors)


def _mode_changed(props, prop, settings):
    _mode_visibility(props, obs.obs_data_get_string(settings, 'audio_mode'), obs.obs_data_get_bool(settings, 'link_colors'))
    return True


def script_properties():
    props = obs.obs_properties_create()
    mode = obs.obs_properties_add_list(props, 'audio_mode', 'Audio', obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_list_add_string(mode, 'MusicBee (mismo audio que el overlay)', 'musicbee')
    obs.obs_property_list_add_string(mode, 'Una fuente de OBS (independiente)', 'obs')
    obs.obs_properties_add_int(props, 'musicbee_port', 'Puerto del overlay de MusicBee', 1024, 65535, 1)
    audio = obs.obs_properties_add_list(props, 'audio_source', 'Fuente de audio OBS', obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    _fill_audio_sources(audio)
    obs.obs_properties_add_button(props, 'refresh_audio_sources', 'Actualizar fuentes de audio', _refresh_audio_sources)
    obs.obs_properties_add_bool(props, 'normalize', 'Normalizar el nivel de las ondas')
    styles = obs.obs_properties_add_list(props, 'style', 'Estilo', obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    for value, label in PRESETS.items():
        obs.obs_property_list_add_string(styles, label, value)
    fft = obs.obs_properties_add_list(props, 'analysis_size', 'Muestras de análisis (FFT)', obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_INT)
    for size in (1024, 2048, 4096, 8192):
        obs.obs_property_list_add_int(fft, str(size), size)
    obs.obs_properties_add_float_slider(props, 'attack_ms', 'Tiempo de subida (ms, 0 = inmediato)', 0.0, 1000.0, 5.0)
    obs.obs_properties_add_float_slider(props, 'release_ms', 'Tiempo de caída (ms, 0 = inmediato)', 0.0, 3000.0, 10.0)
    color_link = obs.obs_properties_add_bool(props, 'link_colors', 'Conectar colores al overlay de MusicBee')
    obs.obs_property_set_modified_callback(color_link, _mode_changed)
    obs.obs_properties_add_color(props, 'color_a', 'Color principal')
    obs.obs_properties_add_color(props, 'color_b', 'Color secundario')
    obs.obs_properties_add_float_slider(props, 'intensity', 'Intensidad', .2, 2.5, .1)
    obs.obs_properties_add_float_slider(props, 'smoothing', 'Suavizado de la forma', 0.0, 1.0, .05)
    obs.obs_properties_add_float_slider(props, 'thickness', 'Grosor', .5, 12.0, .5)
    obs.obs_properties_add_int_slider(props, 'density', 'Cantidad de bandas', 16, 256, 4)
    obs.obs_properties_add_bool(props, 'glow', 'Brillo suave')
    obs.obs_properties_add_bool(props, 'background', 'Fondo sólido (desactivado = transparente)')
    obs.obs_properties_add_color(props, 'background_color', 'Color de fondo')
    obs.obs_properties_add_bool(props, 'visualizer', 'Activar visualizador')
    obs.obs_properties_add_int(props, 'port', 'Puerto de visualizadores', 1024, 65535, 1)
    obs.obs_property_set_modified_callback(mode, _mode_changed)
    _mode_visibility(props, _cfg['audio_mode'])
    return props


def script_update(settings):
    global _audio_seq
    with _lock:
        for key, value in _cfg.items():
            if isinstance(value, bool):
                _cfg[key] = obs.obs_data_get_bool(settings, key)
            elif isinstance(value, float):
                _cfg[key] = obs.obs_data_get_double(settings, key)
            elif isinstance(value, int):
                _cfg[key] = obs.obs_data_get_int(settings, key)
            else:
                _cfg[key] = obs.obs_data_get_string(settings, key)
        if _cfg['style'] not in PRESETS:
            _cfg['style'] = 'ribbon'
        if _cfg['audio_mode'] not in ('musicbee', 'obs'):
            _cfg['audio_mode'] = 'musicbee'
        for key, low, high in [('intensity', .2, 2.5), ('smoothing', 0.0, 1.0), ('thickness', .5, 12.0), ('attack_ms', 0.0, 1000.0), ('release_ms', 0.0, 3000.0)]:
            value = _cfg[key]
            _cfg[key] = min(high, max(low, value)) if math.isfinite(value) else low
        _cfg['density'] = min(256, max(16, _cfg['density']))
        for key, default in [('port', 8766), ('musicbee_port', 8765)]:
            if not 1024 <= _cfg[key] <= 65535:
                _cfg[key] = default
        if _cfg['analysis_size'] not in (1024, 2048, 4096, 8192):
            _cfg['analysis_size'] = 4096
        port = _cfg['port']
        _audio_seq += 1
        _audio_updated.notify_all()
    if _audio_thread is not None and port != _httpd_port:
        _start_server(port)


def _ensure_browser_fps():
    """Solo las fuentes de navegador de este overlay, desde el hilo principal de OBS."""
    with _lock:
        port = _cfg['port']
    sources = obs.obs_enum_sources()
    if not sources:
        return
    try:
        for source in sources:
            if obs.obs_source_get_unversioned_id(source) != 'browser_source':
                continue
            settings = obs.obs_source_get_settings(source)
            try:
                try:
                    url = urlsplit(obs.obs_data_get_string(settings, 'url'))
                    matches = (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')
                               and url.port == port and url.path.rstrip('/') in ('', '/overlay'))
                except ValueError:
                    matches = False
                if matches and (not obs.obs_data_get_bool(settings, 'fps_custom')
                                or obs.obs_data_get_int(settings, 'fps') != 60):
                    obs.obs_data_set_bool(settings, 'fps_custom', True)
                    obs.obs_data_set_int(settings, 'fps', 60)
                    obs.obs_source_update(source, settings)
            finally:
                obs.obs_data_release(settings)
    finally:
        obs.source_list_release(sources)


def _obs_tick():
    global _pending_style, _pending_color_link, _last_browser_scan
    with _lock:
        pending, _pending_style = _pending_style, None
        color_link, _pending_color_link = _pending_color_link, None
    if color_link is not None and _settings_ref is not None:
        obs.obs_data_set_bool(_settings_ref, 'link_colors', color_link)
    if pending and _settings_ref is not None:
        obs.obs_data_set_string(_settings_ref, 'style', pending)
    now = time.monotonic()
    if now - _last_browser_scan >= 1:
        _ensure_browser_fps()
        _last_browser_scan = now


def script_save(settings):
    with _lock:
        style, color_link = _cfg['style'], _cfg['link_colors']
    obs.obs_data_set_string(settings, 'style', style)
    obs.obs_data_set_bool(settings, 'link_colors', color_link)


def script_load(settings):
    global _audio_thread, _settings_ref
    obs.obs_data_addref(settings)
    _settings_ref = settings
    _stop.clear()
    with _lock:
        port = _cfg['port']
    _start_server(port)
    _audio_thread = threading.Thread(target=_audio_worker, daemon=True)
    _audio_thread.start()
    _ensure_browser_fps()
    obs.timer_add(_obs_tick, 100)


def script_unload():
    global _audio_thread, _settings_ref
    obs.timer_remove(_obs_tick)
    _stop.set()
    if _audio_thread is not None:
        _audio_thread.join(timeout=3)
        _audio_thread = None
    _stop_server()
    if _settings_ref is not None:
        obs.obs_data_release(_settings_ref)
        _settings_ref = None
