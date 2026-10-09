"""
MusicBee -> OBS  (misma fuente de datos que usa MusicPresence)

Lee lo que suena a través de los controles multimedia de Windows (SMTC),
saca la carátula y sirve un overlay en http://localhost:PUERTO/ que se añade
a OBS como "Fuente de navegador" (640x180). Incluye una waveform real
calculada directamente con el audio de la fuente que elijas en OBS.
Normaliza automáticamente el nivel de entrada del visualizador, sin cambiar
el volumen de reproducción ni capturar las demás aplicaciones.
Tarjeta horizontal compacta: carátula a la izquierda, título y artista
a la derecha, barra de progreso debajo y tiempo transcurrido / duración total.
El título y el álbum se desplazan de derecha a izquierda si no caben.

Requisitos:
  - Windows 10/11
  - Python 3.12 o inferior configurado en OBS
  - pip install winsdk
  - (waveform real) pip install numpy
  - Seleccionar en el script una fuente de audio de OBS que capture MusicBee.
    No requiere cambiar la salida de MusicBee ni utilizar VB-CABLE.
"""

import asyncio
import ctypes
import json
import math
import os
import queue
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import obspython as obs

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _Manager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _Status,
    )
    from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions
    _IMPORT_ERROR = None
except Exception as e:
    _Manager = _Status = None
    _IMPORT_ERROR = e

POLL_SECONDS = 1.0
MAX_COVER_TRIES = 6
BANDS = 128
WAVE_POINTS = 1024

_lock = threading.Lock()
_audio_updated = threading.Condition(_lock)
_audio_seq = 0
_stop = threading.Event()
_thread = None
_audio_thread = None
_httpd = None
_httpd_port = None

_cfg = {
    "source": "",
    "mode": "smtc",
    "app_filter": "musicbee",
    "format": "{artist} - {title}",
    "paused_text": "",
    "file": os.path.expandvars(r"%appdata%\MusicBee\Tags.txt"),
    "port": 8765,
    "hide_paused": False,
    "visualizer": True,
    "analysis_size": 4096,
    "audio_source": "",
    "capture_muted": True,
    "wave_smoothing": .2, "wave_attack_ms": 18.0, "wave_release_ms": 140.0,
}

_state = {
    "active": False, "playing": False,
    "artist": "", "title": "", "album": "",
    "position": 0.0, "duration": 0.0, "stamp": 0.0,
    "key": None, "cover": None, "mime": "image/jpeg", "tries": 0, "rev": 0,
    "elapsed": 0.0, "tick": 0.0,
    "text": "", "error": None,
}
_bars = [0.0] * BANDS
_audio_ok = False
_wave = [0.0] * WAVE_POINTS
_level = 0.0
_audio_stamp = 0.0
_analysis_data = {}
_audio_error = None
_audio_source_name = ""
_last_applied = None
_last_error = None


# ------------------------------------------------------------ lectura SMTC ---

def _sniff_mime(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


async def _read_cover(props):
    ref = props.thumbnail
    if ref is None:
        return None
    stream = await ref.open_read_async()
    size = stream.size
    if not size:
        return None
    buf = Buffer(size)
    await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
    reader = DataReader.from_buffer(buf)
    data = bytearray(buf.length)
    reader.read_bytes(data)
    return bytes(data)


async def _read_smtc():
    if _Manager is None:
        raise RuntimeError(f"winsdk no disponible: {_IMPORT_ERROR}")

    with _lock:
        app_filter = _cfg["app_filter"].strip().lower()
        prev_key = _state["key"]
        has_cover = _state["cover"] is not None
        tries = _state["tries"]

    manager = await _Manager.request_async()
    for session in manager.get_sessions():
        app_id = (session.source_app_user_model_id or "").lower()
        if app_filter and app_filter not in app_id:
            continue

        props = await session.try_get_media_properties_async()
        playback = session.get_playback_info()
        timeline = session.get_timeline_properties()
        try:
            updated = timeline.last_updated_time
            now = datetime.now(updated.tzinfo) if updated.tzinfo else datetime.utcnow()
            updated_age = (now - updated).total_seconds()
        except Exception:
            updated_age = None
        info = {
            "updated_age": updated_age,
            "artist": props.artist or "",
            "title": props.title or "",
            "album": props.album_title or "",
            "playing": playback.playback_status == _Status.PLAYING,
            "position": timeline.position.total_seconds(),
            "duration": (timeline.end_time - timeline.start_time).total_seconds(),
            "fetched": False,
            "cover": None,
        }
        key = (info["artist"], info["title"], info["album"])
        if key != prev_key or (not has_cover and tries < MAX_COVER_TRIES):
            info["fetched"] = True
            try:
                info["cover"] = await _read_cover(props)
            except Exception:
                info["cover"] = None
        return info
    return None


def _merge_smtc(info):
    with _lock:
        s = _state
        if info is None:
            s.update(active=False, playing=False, text=_cfg["paused_text"])
            return

        now = time.time()
        key = (info["artist"], info["title"], info["album"])
        if key != s["key"]:
            s.update(key=key, cover=None, tries=0, rev=s["rev"] + 1, elapsed=0.0)
        elif s["playing"] and s["active"]:
            s["elapsed"] += now - s["tick"]
        s["tick"] = now

        # Posición: Windows da la posición en el momento de "last_updated_time",
        # así que hay que sumarle el tiempo transcurrido desde entonces.
        dur = info["duration"] if info["duration"] > 0 else 0.0
        lu = info["updated_age"]
        pos = s["elapsed"]  # respaldo si el reproductor no informa de la línea de tiempo
        if dur > 0 and lu is not None:
            est = info["position"] + (lu if info["playing"] else 0.0)
            if 0 <= lu <= dur + 5 and est <= dur + 5:
                pos = est
        if dur > 0:
            pos = min(pos, dur)

        if info["fetched"]:
            s["tries"] += 1
            if info["cover"]:
                s.update(cover=info["cover"], mime=_sniff_mime(info["cover"]),
                         rev=s["rev"] + 1)

        s.update(
            active=True, playing=info["playing"],
            artist=info["artist"], title=info["title"], album=info["album"],
            position=pos, duration=dur, stamp=now,
        )
        if info["playing"]:
            s["text"] = _cfg["format"].format(
                artist=info["artist"], title=info["title"], album=info["album"]
            ).strip(" -")
        else:
            s["text"] = _cfg["paused_text"]


def _merge_file():
    with _lock:
        path = os.path.expandvars(_cfg["file"])
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read().strip()
    with _lock:
        s = _state
        if text != s["title"]:
            s["rev"] += 1
        s.update(active=bool(text), playing=bool(text), title=text, artist="",
                 album="", cover=None, position=0.0, duration=0.0,
                 stamp=time.time(), text=text)


def _worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while not _stop.is_set():
        try:
            with _lock:
                mode = _cfg["mode"]
            if mode == "file":
                _merge_file()
            else:
                _merge_smtc(loop.run_until_complete(_read_smtc()))
            with _lock:
                _state["error"] = None
        except Exception as e:
            with _lock:
                _state["error"] = str(e)
        _stop.wait(POLL_SECONDS)
    loop.close()


# -------------------------------------- fuente de audio OBS / espectro (FFT) ---

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
        self.wave = np.clip(signal[start:start + WAVE_POINTS], -1, 1).round(5).tolist()
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
    def __init__(self, api, name, capture_muted=False):
        self.api, self.requested = api, name
        self.capture_muted = capture_muted
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
            planes = None if muted and not self.capture_muted else tuple(
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
    global _bars, _audio_ok, _audio_source_name, _audio_error, _wave, _level, _audio_stamp, _audio_seq, _analysis_data
    with _audio_updated:
        _bars, _audio_ok, _audio_source_name, _audio_error = bars, ready, source, error
        _wave = list(wave) if wave is not None else [0.0] * WAVE_POINTS
        _level, _audio_stamp = level, time.monotonic()
        _analysis_data = dict(analysis or {})
        _audio_seq += 1
        _audio_updated.notify_all()


def _audio_worker():
    """Analizar exclusivamente una fuente de OBS; no tocar dispositivos ni monitorización."""
    zero, tap = [0.0] * BANDS, None
    try:
        import numpy as np
        api = _ObsAudioApi()
    except Exception as e:
        message = f'{e}. Ejecuta el script dentro de OBS con numpy instalado.'
        _publish_audio(zero, error=message)
        obs.script_log(obs.LOG_WARNING, f'MusicBee NowPlaying: {message}')
        return

    processor = None
    last_packet, inspected, last_error = 0.0, 0.0, None
    try:
        while not _stop.is_set():
            try:
                with _lock:
                    enabled, requested, capture_muted = _cfg['visualizer'], _cfg['audio_source'], _cfg['capture_muted']
                    analysis_size = _cfg['analysis_size']
                if not enabled or not requested:
                    if tap:
                        tap.close()
                        tap = None
                    _publish_audio(zero, error=None if not enabled else 'Selecciona una fuente de audio de OBS para las ondas.')
                    _stop.wait(.1)
                    continue
                if tap and (tap.requested != requested or tap.capture_muted != capture_muted):
                    tap.close()
                    tap = None
                if tap is None:
                    tap = _ObsAudioTap(api, requested, capture_muted=capture_muted)
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
                    bars = processor.process(data)
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
                    obs.script_log(obs.LOG_WARNING, f'MusicBee NowPlaying (fuente OBS): {e}')
                    last_error = str(e)
                _stop.wait(1)
    finally:
        if tap:
            tap.close()
        with _lock:
            error = _audio_error
        _publish_audio(zero, error=error)


# ------------------------------------------------------------- servidor web ---

OVERLAY_HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Now playing</title>
<style>
:root{--accent:#589daf;--accent2:#9cc7cf;--bg:#173344;--ink:#f7f4ef;--ink2:rgba(247,244,239,.65)}
*{box-sizing:border-box;margin:0}
html,body{width:100%;height:100%;background:transparent;overflow:hidden;font-family:system-ui,"Segoe UI",sans-serif}
.card{position:absolute;inset:12px;border-radius:20px;overflow:hidden;padding:13px;
  background:var(--bg);color:var(--ink);display:grid;
  grid-template-columns:min(160px,calc(100vh - 52px),26vw) minmax(0,1fr);gap:20px;
  border:1px solid rgba(255,255,255,.14);box-shadow:0 5px 12px rgba(0,0,0,.22);
  opacity:0;transform:translateY(12px);transition:opacity .5s ease,transform .5s ease,background .8s ease}
.card.on{opacity:1;transform:none}
.backdrop{position:absolute;inset:-30px;background:center/cover no-repeat;
  filter:blur(26px) saturate(.7);opacity:.3;pointer-events:none;transition:background-image .6s}
.glow{position:absolute;inset:0;pointer-events:none;transition:background .8s ease;
  background:linear-gradient(110deg,rgba(8,25,36,.28),rgba(8,25,36,.64))}
.cover{position:relative;align-self:center;width:100%;aspect-ratio:1;border-radius:10px;
  background:#243a46 center/cover no-repeat;overflow:hidden;
  box-shadow:0 3px 10px rgba(0,0,0,.18);transition:opacity .4s}
.cover::after{content:"";position:absolute;inset:0;box-shadow:inset 0 -40px 50px -30px rgba(0,0,0,.55)}
.cover-empty{position:absolute;inset:0;display:grid;place-items:center;color:var(--ink2);font-size:44px}
.cover.has-cover .cover-empty{display:none}
.body{position:relative;align-self:center;display:flex;flex-direction:column;min-width:0;min-height:0;padding-right:18px}
.playback-indicator{position:absolute;top:15px;right:16px;width:12px;height:14px;color:var(--ink2)}
.playback-indicator::before,.playback-indicator::after{content:"";position:absolute;top:1px;width:4px;height:11px;border-radius:1px;background:currentColor}
.playback-indicator::before{left:0}.playback-indicator::after{right:0}
.card.paused .playback-indicator::before{width:0;height:0;top:0;border-top:7px solid transparent;
  border-bottom:7px solid transparent;border-left:11px solid currentColor;border-radius:0;background:none}
.card.paused .playback-indicator::after{display:none}
.progress{margin-top:10px}
.prog{display:flex;justify-content:space-between;gap:10px;margin-top:6px;font-size:11px;line-height:14px;
  color:var(--ink2);font-variant-numeric:tabular-nums}
.bar{height:3px;border-radius:3px;background:rgba(255,255,255,.17);overflow:hidden}
.bar i{display:block;height:100%;width:0;border-radius:2px;
  background:var(--ink);transition:background .8s}
.title{font-size:27px;line-height:33px;font-weight:600;letter-spacing:-.02em}
.artist{font-size:16px;line-height:21px;color:var(--ink2);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.album{margin-top:2px;font-size:11px;line-height:15px;color:var(--ink2);opacity:.8}
.album.empty{display:none}
.marquee{overflow:hidden;white-space:nowrap;min-width:0;
  mask-image:linear-gradient(90deg,#000,#000 calc(100% - 16px),transparent)}
.marquee.fits{mask-image:none}
.marquee-track{display:flex;width:max-content}
.marquee.scrolling .marquee-track{animation:marquee var(--scroll-duration,20s) linear infinite;will-change:transform}
.marquee-track span{flex:none;padding-right:40px}
.marquee.fits .marquee-track span:first-child{padding-right:0}
.marquee.fits .marquee-track span[aria-hidden]{display:none}
@keyframes marquee{from{transform:translateX(0)}to{transform:translateX(calc(-1 * var(--scroll-distance,0px)))}}
canvas{position:absolute;bottom:0;left:0;width:100%;height:44px;opacity:.12;pointer-events:none}
.card.paused .cover,.card.paused .body{opacity:.7}
.card.paused .marquee-track{animation-play-state:paused}
.body{transition:opacity .4s}
.swap{animation:swap .7s cubic-bezier(.2,.8,.2,1)}
@keyframes swap{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@media(max-width:420px){.card{gap:12px;padding:10px;border-radius:16px}
  .body{padding-right:8px}.title{font-size:20px;line-height:25px}.artist{font-size:13px;line-height:18px}
  .album{font-size:10px;line-height:13px}.progress{margin-top:7px}.prog{font-size:10px}}
@media (prefers-reduced-motion:reduce){.swap{animation:none}.card{transition:none}
  .marquee{mask-image:none}.marquee.scrolling .marquee-track{animation:none;width:100%;will-change:auto}
  .marquee-track span:first-child{min-width:0!important;flex:1;overflow:hidden;text-overflow:ellipsis;padding-right:0}
  .marquee-track span[aria-hidden]{display:none}}
</style></head>
<body>
<div class="card" id="card">
  <div class="backdrop" id="backdrop"></div>
  <div class="glow"></div>
  <canvas id="wave"></canvas>
  <div class="cover" id="cover" role="img" aria-label="Portada del álbum"><span class="cover-empty" aria-hidden="true">♫</span></div>
  <div class="body" id="meta">
    <div class="title marquee" id="title"><div class="marquee-track"><span></span><span aria-hidden="true"></span></div></div>
    <div class="artist" id="artist"></div>
    <div class="album marquee empty" id="album"><div class="marquee-track"><span></span><span aria-hidden="true"></span></div></div>
    <div class="progress">
      <div class="bar" id="bar"><i id="fill"></i></div>
      <div class="prog"><span id="cur">0:00</span><span id="tot">—:—</span></div>
    </div>
  </div>
  <div class="playback-indicator" id="status" role="img" aria-label="Reproduciendo" title="Reproduciendo"></div>
</div>
<script>
const $ = id => document.getElementById(id);
const N = 128;
let rev = -1, lastKey = null, playing = false;
let snap = {pos: 0, dur: 0, playing: false, t0: performance.now()};

function swap(el){ el.classList.remove('swap'); void el.offsetWidth; el.classList.add('swap'); }

/* ---------- texto continuo de derecha a izquierda ---------- */
function measureMarquee(el){
  const track = el.firstElementChild, first = track.firstElementChild;
  for(const span of track.children) span.style.minWidth = '';
  // Medir sin la máscara ni el espacio entre copias: un título corto queda fijo.
  el.classList.add('fits'); el.classList.remove('scrolling');
  const overflow = first.getBoundingClientRect().width > el.clientWidth && el.clientWidth > 0;
  el.classList.toggle('fits', !overflow); el.classList.toggle('scrolling', overflow);
  const distance = first.getBoundingClientRect().width;
  track.style.setProperty('--scroll-distance', `${distance}px`);
  track.style.setProperty('--scroll-duration', `${Math.max(8, distance / (el.id === 'title' ? 36 : 28))}s`);
}
function setMarquee(el, text){
  const track = el.firstElementChild;
  for(const span of track.children) span.textContent = text || '';
  el.classList.toggle('empty', !text);
  measureMarquee(el);
  // Reiniciar solo cuando cambia la canción, nunca en cada consulta.
  track.style.animation = 'none'; void track.offsetWidth; track.style.animation = '';
}
const marqueeObserver = new ResizeObserver(entries => {
  for(const entry of entries) measureMarquee(entry.target);
});
for(const id of ['title','album']) marqueeObserver.observe($(id));

/* ---------- color dominante de la caratula ---------- */
function rgb2hsl(r,g,b){
  r/=255;g/=255;b/=255;
  const mx=Math.max(r,g,b), mn=Math.min(r,g,b), d=mx-mn, l=(mx+mn)/2;
  let h=0, s=0;
  if(d){
    s = d/(1-Math.abs(2*l-1));
    if(mx===r) h=((g-b)/d)%6; else if(mx===g) h=(b-r)/d+2; else h=(r-g)/d+4;
    h*=60; if(h<0) h+=360;
  }
  return [h,s,l];
}
function setPalette(h,s){
  const root = document.documentElement.style;
  s = Math.min(1, Math.max(.55, s));
  root.setProperty('--accent',  `hsl(${h} ${s*100}% 46%)`);
  root.setProperty('--accent2', `hsl(${(h+38)%360} ${Math.min(100,s*100+10)}% 62%)`);
  root.setProperty('--bg',      `hsl(${h} 32% 9%)`);
}
function paletteFrom(url){
  const img = new Image();
  img.onload = () => {
    const c = document.createElement('canvas'); c.width = c.height = 32;
    const x = c.getContext('2d'); x.drawImage(img, 0, 0, 32, 32);
    const d = x.getImageData(0, 0, 32, 32).data;
    const bins = new Array(36).fill(0), sat = new Array(36).fill(0);
    for(let i=0;i<d.length;i+=4){
      const [h,s,l] = rgb2hsl(d[i],d[i+1],d[i+2]);
      if(l<.12||l>.9) continue;
      const w = s*s*(1-Math.abs(l-.5));      // favorece colores vivos
      const b = Math.floor(h/10)%36;
      bins[b]+=w; sat[b]+=w*s;
    }
    let best=0; for(let i=1;i<36;i++) if(bins[i]>bins[best]) best=i;
    if(bins[best]>0.5) setPalette(best*10+5, sat[best]/bins[best]);
    else setPalette(255, .35);               // caratula sin color: tono neutro violeta
  };
  img.src = url;
}

/* ---------- datos ---------- */
async function poll(){
  try{
    const s = await (await fetch('/now.json', {cache: 'no-store'})).json();
    const show = s.active && !(s.hide_paused && !s.playing);
    $('card').classList.toggle('on', show);
    $('card').classList.toggle('paused', !s.playing);
    const status = s.playing ? 'Reproduciendo' : 'En pausa';
    $('status').setAttribute('aria-label', status); $('status').title = status;
    playing = s.playing;
    snap = {pos: s.position + s.age, dur: s.duration, playing: s.playing, t0: performance.now()};
    const key = JSON.stringify([s.title, s.artist, s.album]);
    if(key !== lastKey){
      setMarquee($('title'), s.title);
      setMarquee($('album'), s.album);
      $('artist').textContent = s.artist || '';
      $('cover').setAttribute('aria-label', s.album ? `Portada: ${s.album}` : 'Portada del álbum');
      swap($('cover')); swap($('title')); swap($('artist')); swap($('album'));
      lastKey = key;
    }
    if (s.rev !== rev){
      rev = s.rev;
      $('cover').classList.toggle('has-cover', s.has_cover);
      if (s.has_cover){
        const url = `/cover?v=${s.rev}`;
        $('cover').style.backgroundImage = `url(${url})`;
        $('backdrop').style.backgroundImage = `url(${url})`;
        paletteFrom(url);
      } else {
        $('cover').style.backgroundImage = 'none';
        $('backdrop').style.backgroundImage = 'none';
        setPalette(200, .55);
      }
    }
  }catch(e){ $('card').classList.remove('on'); }
}

const fmt = t => { t = Math.max(0, Math.floor(t)); return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0'); };
setInterval(() => {
  let p = snap.playing ? snap.pos + (performance.now() - snap.t0) / 1000 : snap.pos;
  if (snap.dur > 0) p = Math.min(p, snap.dur);
  $('cur').textContent = fmt(p);
  $('tot').textContent = snap.dur > 0 ? fmt(snap.dur) : '—:—';
  $('bar').style.visibility = snap.dur > 0 ? 'visible' : 'hidden';
  $('fill').style.width = snap.dur > 0 ? (p / snap.dur * 100) + '%' : '0%';
}, 250);

/* ---------- waveform ---------- */
let target = new Array(N).fill(0), cur = new Array(N).fill(0), real = false, busy = false;
let response = {smoothing:.2,attack_ms:18,release_ms:140};

async function pollBars(){
  if (busy) return; busy = true;
  try{
    const s = await (await fetch('/spectrum.json', {cache: 'no-store'})).json();
    response = s;
    real = s.real && s.enabled;
    target = real ? s.bars : new Array(N).fill(0);
  }catch(e){ real = false; target = new Array(N).fill(0); } finally { busy = false; }
}
if(window.EventSource){const stream=new EventSource('/audio-events');stream.onmessage=e=>{try{const s=JSON.parse(e.data);response=s;real=s.real&&s.enabled;target=real?s.bars:new Array(N).fill(0);}catch(e){real=false;target.fill(0);}};stream.onerror=()=>{real=false;target.fill(0);};}else setInterval(pollBars,16);

const cv = $('wave'), g = cv.getContext('2d');
let W = 0, H = 0;
function resize(){
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  W = r.width; H = r.height; cv.width = W*dpr; cv.height = H*dpr; g.setTransform(dpr,0,0,dpr,0,0);
}
resize(); addEventListener('resize', resize);

let last = performance.now();
function frame(now){
  const dt = Math.min(.05, (now-last)/1000); last = now;
  if (!W){ resize(); }
  const src = real && playing ? target : new Array(N).fill(0);
  for (let i=0;i<N;i++){
    const t = src[i] || 0;
    const ms = t > cur[i] ? response.attack_ms : response.release_ms;
    const k = ms > 0 ? 1-Math.exp(-dt*1000/ms) : 1;   // sube rapido, cae suave
    cur[i] += (t - cur[i]) * k;
  }
  // suavizado entre bandas vecinas
  const sm = cur.map((v,i) => v*(1-response.smoothing) + response.smoothing*(cur[Math.max(0,i-1)] + 2*v + cur[Math.min(N-1,i+1)]) / 4);
  // simetrica: graves en el centro
  const pts = new Array(N*2);
  for (let i=0;i<N;i++){ pts[N-1-i] = sm[i]; pts[N+i] = sm[i]; }

  g.clearRect(0,0,W,H);
  const mid = H/2, amp = H*.46, step = W/(pts.length-1);
  const xs = pts.map((_,i)=>i*step);
  const up = pts.map((v,i)=>[xs[i], mid - Math.pow(v,.85)*amp]);
  const dn = pts.map((v,i)=>[xs[i], mid + Math.pow(v,.85)*amp*.85]);

  const cs = getComputedStyle(document.documentElement);
  const a1 = cs.getPropertyValue('--accent').trim(), a2 = cs.getPropertyValue('--accent2').trim();
  const grad = g.createLinearGradient(0,0,W,0);
  grad.addColorStop(0, a1); grad.addColorStop(.5, a2); grad.addColorStop(1, a1);

  const path = (arr, rev) => {
    const a = rev ? arr.slice().reverse() : arr;
    g.lineTo(a[0][0], a[0][1]);
    for (let i=1;i<a.length-1;i++){
      const mx = (a[i][0]+a[i+1][0])/2, my = (a[i][1]+a[i+1][1])/2;
      g.quadraticCurveTo(a[i][0], a[i][1], mx, my);
    }
    g.lineTo(a[a.length-1][0], a[a.length-1][1]);
  };
  g.beginPath(); g.moveTo(up[0][0], mid);
  path(up,false); g.lineTo(xs[xs.length-1], mid);
  path(dn,true);  g.closePath();
  g.shadowColor = a1; g.shadowBlur = 18;
  g.globalAlpha = .85; g.fillStyle = grad; g.fill();
  g.shadowBlur = 0; g.globalAlpha = 1;

  // linea de brillo superior
  g.beginPath(); g.moveTo(up[0][0], up[0][1]); path(up,false);
  g.strokeStyle = 'rgba(255,255,255,.55)'; g.lineWidth = 1.2; g.stroke();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

poll(); setInterval(poll, 1000);
</script></body></html>
"""


def _audio_snapshot():
    with _lock:
        return {
            "bars": list(_bars), "real": bool(_audio_ok and _cfg["visualizer"] and time.monotonic() - _audio_stamp < .3),
            "enabled": _cfg["visualizer"], "source": _audio_source_name,
            "wave": list(_wave), "level": _level,
            "age": max(0.0, time.monotonic() - _audio_stamp),
            "error": _audio_error, "analysis": dict(_analysis_data),
            "smoothing": _cfg["wave_smoothing"],
            "attack_ms": _cfg["wave_attack_ms"], "release_ms": _cfg["wave_release_ms"],
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
            next_frame = time.monotonic() + 1 / 120
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/audio-events":
                _stream_state(self, _audio_snapshot)
                return
            if path in ("/", "/overlay", "/overlay.html"):
                self._send(200, "text/html; charset=utf-8", OVERLAY_HTML.encode("utf-8"))
            elif path == "/now.json":
                with _lock:
                    s = _state
                    payload = {
                        "active": s["active"], "playing": s["playing"],
                        "artist": s["artist"], "title": s["title"], "album": s["album"],
                        "position": s["position"], "duration": s["duration"],
                        "age": max(0.0, time.time() - s["stamp"]) if s["playing"] else 0.0,
                        "rev": s["rev"], "has_cover": s["cover"] is not None,
                        "hide_paused": _cfg["hide_paused"],
                    }
                self._send(200, "application/json", json.dumps(payload).encode("utf-8"))
            elif path == "/spectrum.json":
                payload = _audio_snapshot()
                self._send(200, "application/json", json.dumps(payload).encode("utf-8"))
            elif path == "/cover":
                with _lock:
                    cover, mime = _state["cover"], _state["mime"]
                if cover:
                    self._send(200, mime, cover)
                else:
                    self._send(404, "text/plain", b"sin caratula")
            else:
                self._send(404, "text/plain", b"no encontrado")
        except (BrokenPipeError, ConnectionResetError):
            pass


def _start_server(port):
    global _httpd, _httpd_port
    _stop_server()
    try:
        _httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        _httpd = None
        obs.script_log(obs.LOG_WARNING, f"MusicBee NowPlaying: no se pudo abrir el puerto {port}: {e}")
        return
    _httpd.stream_stop = threading.Event()
    _httpd_port = _httpd.server_port
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()


def _stop_server():
    global _httpd, _httpd_port
    if _httpd is not None:
        _httpd.stream_stop.set()
        with _audio_updated:
            _audio_updated.notify_all()
        _httpd.shutdown()
        _httpd.server_close()
    _httpd, _httpd_port = None, None


# ----------------------------------------------------------- fuente de texto ---

def _apply():
    """Timer en el hilo principal de OBS."""
    global _last_applied, _last_error

    with _lock:
        text, error, name = _state["text"], _state["error"], _cfg["source"]

    if error and error != _last_error:
        obs.script_log(obs.LOG_WARNING, f"MusicBee NowPlaying: {error}")
    _last_error = error

    if not name or text == _last_applied:
        return
    source = obs.obs_get_source_by_name(name)
    if source is None:
        return
    data = obs.obs_data_create()
    obs.obs_data_set_string(data, "text", text)
    obs.obs_source_update(source, data)
    obs.obs_data_release(data)
    obs.obs_source_release(source)
    _last_applied = text


# -------------------------------------------------------------- API de OBS ---

def script_description():
    return (
        "<b>MusicBee → OBS</b><br>"
        "Overlay con carátula y waveform en <code>http://localhost:PUERTO/</code> "
        "(añádelo como Fuente de navegador, 640×180). Tarjeta compacta, carátula "
        "a la izquierda, título/artista y barra de progreso con tiempo y duración. "
        "Lee los controles multimedia "
        "de Windows, igual que MusicPresence. Audio de la fuente de OBS que elijas con "
        "normalización automática para las ondas (sin cambiar el volumen audible). "
        "Compatible con Windows 10/11. Selecciona una fuente de audio de OBS "
        "en las propiedades. Instala numpy en el Python de OBS."
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
    obs.obs_property_list_add_string(prop, "(selecciona una fuente de audio)", "")
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
    _fill_audio_sources(obs.obs_properties_get(props, "audio_source"))
    return True


def script_properties():
    props = obs.obs_properties_create()

    obs.obs_properties_add_int(props, "port", "Puerto del overlay", 1024, 65535, 1)
    obs.obs_properties_add_bool(props, "hide_paused", "Ocultar overlay en pausa")
    obs.obs_properties_add_bool(props, "visualizer", "Ondas de MusicBee (nivel normalizado)")
    obs.obs_properties_add_bool(props, "capture_muted", "Capturar aunque la fuente esté silenciada en OBS")
    fft = obs.obs_properties_add_list(props, "analysis_size", "Muestras de análisis (FFT)", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_INT)
    for size in (1024, 2048, 4096, 8192):
        obs.obs_property_list_add_int(fft, str(size), size)
    obs.obs_properties_add_float_slider(props, "wave_smoothing", "Suavizado de la forma", 0, 1, .05)
    obs.obs_properties_add_float_slider(props, "wave_attack_ms", "Tiempo de subida (ms, 0 = inmediato)", 0, 1000, 5)
    obs.obs_properties_add_float_slider(props, "wave_release_ms", "Tiempo de caída (ms, 0 = inmediato)", 0, 3000, 10)
    audio = obs.obs_properties_add_list(
        props, "audio_source", "Fuente de audio OBS para las ondas",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING,
    )
    _fill_audio_sources(audio)
    obs.obs_properties_add_button(props, "refresh_audio_sources", "Actualizar fuentes de audio", _refresh_audio_sources)

    lst = obs.obs_properties_add_list(
        props, "source", "Fuente de texto (opcional)",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_list_add_string(lst, "(ninguna)", "")
    sources = obs.obs_enum_sources()
    if sources:
        for s in sources:
            if obs.obs_source_get_unversioned_id(s).startswith("text_"):
                name = obs.obs_source_get_name(s)
                obs.obs_property_list_add_string(lst, name, name)
        obs.source_list_release(sources)

    mode = obs.obs_properties_add_list(
        props, "mode", "Modo",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_list_add_string(mode, "Controles multimedia de Windows (como MusicPresence)", "smtc")
    obs.obs_property_list_add_string(mode, "Archivo de texto (plugin de MusicBee, sin carátula)", "file")

    obs.obs_properties_add_text(props, "app_filter", "Filtrar por aplicación", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "format", "Formato del texto ({artist} {title} {album})", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "paused_text", "Texto si está en pausa", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_path(props, "file", "Archivo (solo modo archivo)",
                                obs.OBS_PATH_FILE, "Texto (*.txt)", None)
    return props


def script_update(settings):
    global _last_applied, _audio_seq
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
        if _cfg["analysis_size"] not in (1024, 2048, 4096, 8192):
            _cfg["analysis_size"] = 4096
        for key, low, high in [("wave_smoothing", 0, 1), ("wave_attack_ms", 0, 1000), ("wave_release_ms", 0, 3000)]:
            value = _cfg[key]
            _cfg[key] = min(high, max(low, value)) if math.isfinite(value) else low
        port = _cfg["port"]
        # forzar que se vuelva a leer la carátula con el nuevo filtro
        _state.update(key=None, cover=None, tries=0)
        _audio_seq += 1
        _audio_updated.notify_all()
    _last_applied = None
    if _thread is not None and port != _httpd_port:
        _start_server(port)


def script_load(settings):
    global _thread, _audio_thread
    if _IMPORT_ERROR:
        obs.script_log(
            obs.LOG_WARNING,
            f"No se pudo importar winsdk ({_IMPORT_ERROR}). "
            "Instálalo con 'pip install winsdk' o usa el modo archivo.",
        )
    _stop.clear()
    _thread = threading.Thread(target=_worker, daemon=True)
    _thread.start()
    _audio_thread = threading.Thread(target=_audio_worker, daemon=True)
    _audio_thread.start()
    with _lock:
        port = _cfg["port"]
    _start_server(port)
    obs.timer_add(_apply, 500)


def script_unload():
    obs.timer_remove(_apply)
    _stop.set()
    _stop_server()
    if _thread:
        _thread.join(timeout=2)
    if _audio_thread:
        _audio_thread.join(timeout=2)
