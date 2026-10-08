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
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

import obspython as obs

BANDS = 64
WAVE_POINTS = 256
PRESETS = {
    "bars": "Barras clásicas",
    "mirror": "Barras espejo",
    "ribbon": "Ondas suaves",
    "scope": "Osciloscopio",
    "ring": "Espectro circular",
    "orbit": "Anillo fluido",
    "dots": "Barras LED",
    "particles": "Partículas",
}
_cfg = {
    "audio_mode": "musicbee", "musicbee_port": 8765,
    "audio_source": "", "port": 8766, "style": "ribbon",
    "visualizer": True, "normalize": True,
    "color_a": 0xFFFFE156, "color_b": 0xFFFF72AD, "background_color": 0xFF181008,
    "background": False, "glow": True,
    "intensity": 1.0, "smoothing": .7, "thickness": 3.0, "density": 48,
}
_lock = threading.Lock()
_stop = threading.Event()
_audio_thread = None
_httpd = None
_httpd_port = None
_audio_ok = False
_audio_error = None
_audio_source_name = ""
_audio = {"bands": [0.0] * BANDS, "wave": [0.0] * WAVE_POINTS, "level": 0.0, "stamp": 0.0}


class _SpectrumProcessor:
    """AGC RMS solo para las ondas: nivel objetivo, límite de ganancia y puerta de silencio."""
    def __init__(self, np, rate=44100, n=2048):
        self.np, self.rate, self.n = np, rate, n
        self.win = np.hanning(n).astype(np.float32)[:, None]
        self.buf = np.zeros((n, 2), dtype=np.float32)
        self.gain = None
        self.wave = [0.0] * WAVE_POINTS
        self.level = 0.0
        edges = np.geomspace(40, min(14000, rate * .49), BANDS + 1)
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
            return [0.0] * BANDS
        data = np.nan_to_num(np.asarray(data, dtype=np.float32), nan=0, posinf=0, neginf=0)
        if self.buf.shape[1] != data.shape[1]:
            self.buf = np.zeros((self.n, data.shape[1]), dtype=np.float32)
            self.gain = None
        rms = float(np.sqrt(np.mean(data * data)))
        if rms <= .0001:  # ~-80 dBFS: no convertir ruido o silencio en ondas.
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
        # Limitar inmediatamente los picos, subir suavemente en pasajes más bajos.
        if normalize:
            self.gain = min(self.gain, .98 / peak)
        samples = data[-self.n:] * self.gain
        count = len(samples)
        self.buf[:-count] = self.buf[count:]
        self.buf[-count:] = samples
        self.level = min(1.0, float(np.sqrt(np.mean(samples * samples))) * 4)
        # Osciloscopio real: canal con más energía, ventana alineada al cruce por cero.
        channel = int(np.argmax(np.mean(self.buf * self.buf, axis=0)))
        signal = self.buf[:, channel]
        length = min(768, self.n)
        limit = self.n - length
        crossings = np.flatnonzero((signal[:limit + 1] <= 0) & (signal[1:limit + 2] > 0))
        start = int(crossings[-1]) if len(crossings) else limit
        window = signal[start:start + length]
        self.wave = np.clip(np.interp(np.linspace(0, length - 1, WAVE_POINTS),
                                     np.arange(length), window), -1, 1).round(4).tolist()
        # Energía de ambos canales: una señal estéreo en contrafase no desaparece.
        fft = np.fft.rfft(self.buf * self.win, axis=0)
        mag = np.sqrt(np.mean(np.abs(fft) ** 2, axis=1)) / (self.n / 4)
        out = []
        for i, bins in enumerate(self.idx):
            db = 20 * np.log10(float(mag[bins].max()) + 1e-9)
            out.append(round(min(1.0, max(0.0, (db + 72) / 52 + self.tilt[i])), 3))
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
        self.packets = queue.Queue(maxsize=4)
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
        # de que OBS reutilice los buffers; la cola descarta audio antiguo.
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
                except queue.Empty:
                    pass
                try:
                    self.packets.put_nowait(planes)
                except queue.Full:
                    pass
        except Exception as e:
            self.error = str(e)

    def read(self, np):
        planes = self.packets.get(timeout=.05)
        # Usar el paquete más reciente para mantener baja la latencia.
        while True:
            try:
                planes = self.packets.get_nowait()
            except queue.Empty:
                break
        if planes is None:
            return None  # Fuente silenciada.
        return np.column_stack([np.frombuffer(p, dtype=np.float32) for p in planes])

    def close(self):
        self.active = False
        if self.registered:
            self.api.obs_source_remove_audio_capture_callback(self.source, self.callback, None)
            self.registered = False
        if self.source:
            self.api.obs_source_release(self.source)
            self.source = None


def _publish_audio(bars, ready=False, source='', error=None, wave=None, level=0.0):
    global _audio_ok, _audio_source_name, _audio_error
    with _lock:
        _audio_ok, _audio_source_name, _audio_error = ready, source, error
        _audio.update(bands=list(bars), wave=list(wave) if wave is not None else [0.0] * WAVE_POINTS,
                      level=level, stamp=time.monotonic())


_local_http = build_opener(ProxyHandler({}))


def _read_musicbee(port):
    if port == _httpd_port:
        raise RuntimeError('El puerto de MusicBee debe ser distinto del puerto de visualizadores.')
    with _local_http.open(f'http://127.0.0.1:{port}/spectrum.json', timeout=.75) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise RuntimeError('La respuesta de MusicBee es demasiado grande.')
    data = json.loads(raw)
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
    _publish_audio(numbers(bands, 0, 1), True, source, wave=numbers(wave, -1, 1), level=level)


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
                    _read_musicbee(musicbee_port)
                    last_error = None
                    _stop.wait(1 / 30)
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
                    processor = _SpectrumProcessor(np, rate=tap.rate)
                    last_packet = inspected = time.monotonic()
                    _publish_audio(zero, source=requested)
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
                _publish_audio(bars, True, requested, wave=processor.wave, level=processor.level)
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
header,.hero-bar,.grid,footer{display:none}
body.gallery{background:#0b1020;overflow:auto;height:auto;min-height:100vh;
 background-image:radial-gradient(ellipse at 8% 0%,rgba(86,225,255,.08),transparent 45%),radial-gradient(ellipse at 100% 50%,rgba(173,114,255,.08),transparent 45%)}
.gallery #app{max-width:1240px;height:auto;margin:auto;padding:30px}
.gallery header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:22px}
.kicker{color:var(--a);font-size:10px;letter-spacing:.2em;font-weight:700;margin:0 0 8px}
h1{font-size:30px;line-height:1.1;letter-spacing:-.04em;margin:0}header p{font-size:13px;color:var(--muted);margin:10px 0 0}
.demo{display:flex;align-items:center;gap:9px;padding:10px 14px;border:1px solid var(--line);border-radius:999px;font-size:12px;white-space:nowrap;cursor:pointer}
.demo input{accent-color:var(--a);width:15px;height:15px}
.gallery .hero{height:auto;overflow:hidden;border:1px solid var(--line);border-radius:18px;background:rgba(5,10,21,.7)}
.gallery .hero-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 20px;border-bottom:1px solid rgba(255,255,255,.05)}
.hero-title{font-size:14px;font-weight:600}#signal{font-size:11px;color:var(--muted)}#signal.demo-on{color:#ffcb78}
.gallery #main{height:220px}
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
<header><div><p class="kicker">AUDIO / OBS</p><h1>Encuentra tu ritmo.</h1><p>Ocho formas de ver la misma música. Elige la que más te guste.</p></div>
<label class="demo"><input id="demo" type="checkbox">Comparar con demostración</label></header>
<section class="hero"><div class="hero-bar"><span class="hero-title" id="selected">Ondas suaves</span><span id="signal">Esperando audio de MusicBee</span></div><canvas id="main" aria-label="Visualizador de audio"></canvas></section>
<section class="grid" id="grid" aria-label="Estilos de visualizador"></section>
<footer>Para usar tu elección, selecciona <strong id="choice-label">Ondas suaves</strong> en <strong>Estilo</strong>, en las propiedades del script de OBS. La demostración solo se muestra en esta galería.</footer>
</main>
<script>
const STYLES={bars:'Barras clásicas',mirror:'Barras espejo',ribbon:'Ondas suaves',scope:'Osciloscopio',ring:'Espectro circular',orbit:'Anillo fluido',dots:'Barras LED',particles:'Partículas'};
const gallery=location.pathname.replace(/\/$/,'')==='/compare';
if(gallery)document.body.classList.add('gallery');
const queryStyle=new URLSearchParams(location.search).get('style');
let selection=STYLES[queryStyle]?queryStyle:'ribbon', clicked=false;
let config={style:'ribbon',color_a:'#56e1ff',color_b:'#ad72ff',background_color:'#081018',background:false,glow:true,intensity:1,smoothing:.7,thickness:3,density:48};
let packet={bands:new Array(64).fill(0),wave:new Array(256).fill(0),level:0,ready:false,source:'',error:null};
let received=0, demo=false;
const clamp=(v,a=0,b=1)=>Math.min(b,Math.max(a,Number.isFinite(v)?v:0));
function resample(values,n){const last=values.length-1;return Array.from({length:n},(_,i)=>{const p=i*last/(n-1),j=Math.floor(p);return (values[j]||0)*(1-p+j)+(values[Math.min(last,j+1)]||0)*(p-j)});}
function curve(g,points){g.moveTo(...points[0]);for(let i=1;i<points.length-1;i++){g.quadraticCurveTo(...points[i],(points[i][0]+points[i+1][0])/2,(points[i][1]+points[i+1][1])/2);}g.lineTo(...points[points.length-1]);}
function rounded(g,x,y,w,h,r){r=Math.min(r,w/2,h/2);g.beginPath();g.moveTo(x+r,y);g.arcTo(x+w,y,x+w,y+h,r);g.arcTo(x+w,y+h,x,y+h,r);g.arcTo(x,y+h,x,y,r);g.arcTo(x,y,x+w,y,r);g.closePath();g.fill();}
class Visualizer{
 constructor(canvas,style){this.canvas=canvas;this.g=canvas.getContext('2d');this.style=style;this.v=new Array(64).fill(0);this.wave=new Array(256).fill(0);this.level=0;this.phase=0;this.w=0;this.h=0;this.key='';this.observer=new ResizeObserver(()=>this.resize());this.observer.observe(canvas);this.resize();}
 resize(){const r=this.canvas.getBoundingClientRect();this.w=r.width;this.h=r.height;const d=Math.min(window.devicePixelRatio||1,gallery?1.5:2);this.canvas.width=Math.round(this.w*d);this.canvas.height=Math.round(this.h*d);this.g.setTransform(d,0,0,d,0,0);this.key='';}
 draw(data,dt,c){
  const {g,w,h}=this;if(!w||!h)return;
  const live=data.ready, bands=live?resample(data.bands,64):new Array(64).fill(0), wave=live?resample(data.wave,256):new Array(256).fill(0);
  const smooth=clamp(c.smoothing), attack=1-Math.exp(-dt*(50-35*smooth)),release=1-Math.exp(-dt*(28-23*smooth));
  for(let i=0;i<64;i++){const t=clamp(bands[i]);this.v[i]+=(t-this.v[i])*(t>this.v[i]?attack:release);}
  for(let i=0;i<256;i++)this.wave[i]+=(clamp(wave[i],-1,1)-this.wave[i])*(1-Math.exp(-dt*45));
  this.level+=(clamp(live?data.level:0)-this.level)*attack;this.phase+=dt*this.level*.6;
  g.clearRect(0,0,w,h);g.globalAlpha=1;g.lineCap='round';g.lineJoin='round';
  const key=`${w},${h},${c.color_a},${c.color_b}`;if(key!==this.key){this.gradient=g.createLinearGradient(w*.08,h,w*.92,0);this.gradient.addColorStop(0,c.color_a);this.gradient.addColorStop(1,c.color_b);this.key=key;}
  g.fillStyle=g.strokeStyle=this.gradient;g.shadowColor=c.color_a;g.shadowBlur=c.glow?Math.min(18,h*.08):0;g.lineWidth=c.thickness;
  const n=Math.round(clamp(c.density,16,96)), values=resample(this.v,n).map(v=>clamp(v*c.intensity)), pad=w*.07, width=w-2*pad;
  const mirrored=()=>{const half=resample(this.v,Math.ceil(n/2)).map(v=>clamp(v*c.intensity));return half.slice().reverse().concat(half);};
  if(this.style==='bars'||this.style==='mirror'){
   const vals=this.style==='mirror'?mirrored():values;const step=width/vals.length,bw=Math.max(1,step*.68);
   vals.forEach((v,i)=>{const size=Math.max(1.5,v*h*(this.style==='mirror'?.36:.72));const y=this.style==='mirror'?h/2-size:h*.86-size;rounded(g,pad+i*step,y,bw,this.style==='mirror'?2*size:size,Math.min(3,bw/2));});
  }else if(this.style==='ribbon'){
   const vals=mirrored(),step=width/(vals.length-1),mid=h/2;
   const top=vals.map((v,i)=>[pad+i*step,mid-Math.pow(v,.9)*h*.35]);const bottom=vals.map((v,i)=>[pad+i*step,mid+Math.pow(v,.9)*h*.29]).reverse();
   g.beginPath();curve(g,top);g.lineTo(...bottom[0]);for(let i=1;i<bottom.length-1;i++)g.quadraticCurveTo(...bottom[i],(bottom[i][0]+bottom[i+1][0])/2,(bottom[i][1]+bottom[i+1][1])/2);g.lineTo(...bottom[bottom.length-1]);g.closePath();g.globalAlpha=.85;g.fill();g.globalAlpha=1;
   g.beginPath();curve(g,top);g.strokeStyle='rgba(255,255,255,.65)';g.lineWidth=Math.max(1,c.thickness*.4);g.stroke();
  }else if(this.style==='scope'){
   const pts=this.wave.map((v,i)=>[pad+i*width/255,h/2-clamp(v*3.5*c.intensity,-1,1)*h*.38]);
   g.beginPath();curve(g,pts);g.stroke();
  }else if(this.style==='ring'){
   const radius=Math.min(w,h)*.27,cx=w/2,cy=h/2;g.globalAlpha=.18;g.beginPath();g.arc(cx,cy,radius,0,Math.PI*2);g.stroke();g.globalAlpha=1;
   values.forEach((v,i)=>{const a=i/n*Math.PI*2-Math.PI/2,extent=radius+v*Math.min(w,h)*.18;g.lineWidth=Math.max(1,Math.min(c.thickness+1,radius*4/n));g.beginPath();g.moveTo(cx+Math.cos(a)*radius,cy+Math.sin(a)*radius);g.lineTo(cx+Math.cos(a)*extent,cy+Math.sin(a)*extent);g.stroke();});
  }else if(this.style==='orbit'){
   const size=Math.min(w,h),base=size*(.22+.025*this.level),cx=w/2,cy=h/2;
   for(let layer=2;layer>=0;layer--){const pts=values.map((v,i)=>{const a=i/n*Math.PI*2-Math.PI/2,rad=base+v*size*(.12+layer*.035);return[cx+Math.cos(a)*rad,cy+Math.sin(a)*rad];});pts.push(pts[0],pts[1]);g.beginPath();curve(g,pts);g.closePath();g.globalAlpha=layer===0?.9:.14;g.lineWidth=layer===0?c.thickness:c.thickness*2;g.stroke();}g.globalAlpha=1;
  }else if(this.style==='dots'){
   const rows=12,step=width/n,dy=h*.72/rows,r=Math.max(.8,Math.min(step*.27,dy*.32));g.shadowBlur=c.glow?8:0;
   values.forEach((v,i)=>{const lit=Math.round(v*rows);for(let j=0;j<rows;j++){g.globalAlpha=j<lit?1:.07;g.beginPath();g.arc(pad+(i+.5)*step,h*.86-j*dy,r,0,Math.PI*2);g.fill();}});g.globalAlpha=1;
  }else if(this.style==='particles'){
   const size=Math.min(w,h),cx=w/2,cy=h/2;
   for(let i=0;i<96;i++){const v=values[i%n];if(v<.01)continue;const angle=i*2.399963+this.phase*((i%2)?1:-1),rad=size*(.06+.3*Math.sqrt((i+.5)/96))*(.65+.55*v);
    const x=cx+Math.cos(angle)*rad*(w>h?Math.min(2,w/h):1),y=cy+Math.sin(angle)*rad;g.globalAlpha=clamp(v*.95);g.beginPath();g.arc(x,y,Math.max(.7,(1+i%4)*v*size*.012),0,Math.PI*2);g.fill();}g.globalAlpha=1;
  }
  g.shadowBlur=0;g.globalAlpha=1;
 }
}
const main=new Visualizer(document.getElementById('main'),selection),renderers=[main];
function select(style){selection=style;main.style=style;document.getElementById('selected').textContent=STYLES[style];document.getElementById('choice-label').textContent=STYLES[style];document.querySelectorAll('.preset').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.style===style)));}
if(gallery){
 for(const [id,label]of Object.entries(STYLES)){const button=document.createElement('button');button.type='button';button.className='preset';button.dataset.style=id;button.setAttribute('aria-label',label);const canvas=document.createElement('canvas');canvas.setAttribute('aria-hidden','true');const caption=document.createElement('div');caption.className='preset-label';const name=document.createElement('span');name.textContent=label;const check=document.createElement('span');check.className='choice';check.textContent='Elegido';caption.append(name,check);button.append(canvas,caption);document.getElementById('grid').append(button);renderers.push(new Visualizer(canvas,id));button.addEventListener('click',()=>{clicked=true;select(id);});}
 document.getElementById('demo').addEventListener('change',e=>{demo=e.target.checked;});select(selection);
}
async function poll(){
 try{const response=await fetch('/state.json',{cache:'no-store'});if(!response.ok)throw Error('No disponible');const s=await response.json();packet=s;config=s.config;received=performance.now();document.documentElement.style.setProperty('--a',config.color_a);document.documentElement.style.setProperty('--b',config.color_b);
  if(!gallery){main.style=STYLES[queryStyle]?queryStyle:config.style;document.body.style.background=config.background?config.background_color:'transparent';}
  else if(!clicked&&!STYLES[queryStyle])select(config.style);
 }catch(e){packet.ready=false;}finally{setTimeout(poll,33);}
}
function demonstration(t){return{ready:true,bands:Array.from({length:64},(_,i)=>clamp((.3+.27*Math.sin(t*2.1-i*.32)+.15*Math.sin(t*.8+i*.65))*(1-i*.008))),wave:Array.from({length:256},(_,i)=>.2*Math.sin(i*.19+t)+.055*Math.sin(i*.57-t)),level:.4+.08*Math.sin(t*2)};}
let previous=performance.now();
function frame(now){const dt=Math.min(.05,(now-previous)/1000);previous=now;let data=gallery&&demo?demonstration(now/1000):{...packet,ready:packet.ready&&now-received<700};
 for(const renderer of renderers)renderer.draw(data,dt,config);
 if(gallery){const signal=document.getElementById('signal');signal.classList.toggle('demo-on',demo);signal.textContent=demo?'Demostración · sin audio real':data.ready?`Audio de OBS · ${data.source||'MusicBee'}`:'Esperando audio de MusicBee';}
 requestAnimationFrame(frame);
}
poll();requestAnimationFrame(frame);
</script></body></html>
"""


def _css_color(value):
    return '#{:02x}{:02x}{:02x}'.format(value & 255, (value >> 8) & 255, (value >> 16) & 255)


def _snapshot():
    with _lock:
        config = {key: _cfg[key] for key in ('style', 'background', 'glow', 'intensity', 'smoothing', 'thickness', 'density')}
        config.update({key: _css_color(_cfg[key]) for key in ('color_a', 'color_b', 'background_color')})
        ready = bool(_audio_ok and _cfg['visualizer'] and time.monotonic() - _audio['stamp'] < .3)
        return {
            'ready': ready, 'source': _audio_source_name, 'error': _audio_error,
            'bands': list(_audio['bands']) if ready else [0.0] * BANDS,
            'wave': list(_audio['wave']) if ready else [0.0] * WAVE_POINTS,
            'level': _audio['level'] if ready else 0.0, 'config': config,
        }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip('/') or '/'
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
        _httpd.shutdown()
        _httpd.server_close()
    _httpd, _httpd_port = None, None


def _start_server(port):
    global _httpd, _httpd_port
    _stop_server()
    try:
        _httpd = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
        _httpd_port = _httpd.server_port
        threading.Thread(target=_httpd.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
    except OSError as e:
        _httpd = None
        obs.script_log(obs.LOG_WARNING, f'OBS Visualizers: no se pudo abrir el puerto {port}: {e}')


def script_description():
    return (
        '<b>OBS Visualizers</b><br>Ocho visualizadores para el audio de MusicBee. '
        'Por defecto comparten el audio de musicbee_nowplaying.py (puerto 8765). '
        'Carga ambos scripts para usar ese modo.<br>'
        'Fuente de navegador: <code>http://localhost:8766/</code>, 900×300, '
        'o 600×600 para los estilos circulares. '
        'Compara estilos en <code>http://localhost:8766/compare</code>.<br>'
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


def _mode_visibility(props, mode):
    for name in ('audio_source', 'refresh_audio_sources', 'normalize'):
        obs.obs_property_set_visible(obs.obs_properties_get(props, name), mode == 'obs')
    obs.obs_property_set_visible(obs.obs_properties_get(props, 'musicbee_port'), mode == 'musicbee')


def _mode_changed(props, prop, settings):
    _mode_visibility(props, obs.obs_data_get_string(settings, 'audio_mode'))
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
    obs.obs_properties_add_color(props, 'color_a', 'Color principal')
    obs.obs_properties_add_color(props, 'color_b', 'Color secundario')
    obs.obs_properties_add_float_slider(props, 'intensity', 'Intensidad', .2, 2.5, .1)
    obs.obs_properties_add_float_slider(props, 'smoothing', 'Suavizado', 0.0, 1.0, .05)
    obs.obs_properties_add_float_slider(props, 'thickness', 'Grosor', .5, 12.0, .5)
    obs.obs_properties_add_int_slider(props, 'density', 'Cantidad de bandas', 16, 96, 4)
    obs.obs_properties_add_bool(props, 'glow', 'Brillo suave')
    obs.obs_properties_add_bool(props, 'background', 'Fondo sólido (desactivado = transparente)')
    obs.obs_properties_add_color(props, 'background_color', 'Color de fondo')
    obs.obs_properties_add_bool(props, 'visualizer', 'Activar visualizador')
    obs.obs_properties_add_int(props, 'port', 'Puerto de visualizadores', 1024, 65535, 1)
    obs.obs_property_set_modified_callback(mode, _mode_changed)
    _mode_visibility(props, _cfg['audio_mode'])
    return props


def script_update(settings):
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
        for key, low, high in [('intensity', .2, 2.5), ('smoothing', 0.0, 1.0), ('thickness', .5, 12.0)]:
            value = _cfg[key]
            _cfg[key] = min(high, max(low, value)) if math.isfinite(value) else low
        _cfg['density'] = min(96, max(16, _cfg['density']))
        for key, default in [('port', 8766), ('musicbee_port', 8765)]:
            if not 1024 <= _cfg[key] <= 65535:
                _cfg[key] = default
        port = _cfg['port']
    if _audio_thread is not None and port != _httpd_port:
        _start_server(port)


def script_load(settings):
    global _audio_thread
    _stop.clear()
    with _lock:
        port = _cfg['port']
    _start_server(port)
    _audio_thread = threading.Thread(target=_audio_worker, daemon=True)
    _audio_thread.start()


def script_unload():
    global _audio_thread
    _stop.set()
    if _audio_thread is not None:
        _audio_thread.join(timeout=3)
        _audio_thread = None
    _stop_server()
