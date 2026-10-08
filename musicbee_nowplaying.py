"""
MusicBee -> OBS  (misma fuente de datos que usa MusicPresence)

Lee lo que suena a través de los controles multimedia de Windows (SMTC),
saca la carátula y sirve un overlay en http://localhost:PUERTO/ que se añade
a OBS como "Fuente de navegador" (640x180). Incluye una waveform real
calculada con el audio del sistema (loopback).
Tarjeta horizontal compacta: carátula a la izquierda, título y artista
a la derecha, barra de progreso debajo y tiempo transcurrido / duración total.
El título y el álbum se desplazan de derecha a izquierda si no caben.

Requisitos:
  - Windows 10/11
  - Python 3.12 o inferior configurado en OBS
  - pip install winsdk
  - (waveform real) pip install soundcard numpy
"""

import asyncio
import json
import os
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
BANDS = 48

_lock = threading.Lock()
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


# ------------------------------------------------- audio / espectro (FFT) ---

def _audio_worker():
    """Captura el audio del sistema (loopback) y calcula BANDS bandas."""
    global _bars, _audio_ok
    try:
        import numpy as np
        import soundcard as sc
    except Exception as e:
        obs.script_log(obs.LOG_INFO,
                       f"MusicBee NowPlaying: sin waveform real ({e}). "
                       "Instala 'soundcard' y 'numpy' o se usará una animación simulada.")
        return

    rate, n = 44100, 2048
    win = np.hanning(n)
    edges = np.geomspace(40, 14000, BANDS + 1)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    idx = [np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0] for i in range(BANDS)]
    # las bandas muy graves pueden quedarse sin bins: usar el más cercano
    for i in range(BANDS):
        if len(idx[i]) == 0:
            idx[i] = np.array([int(np.argmin(np.abs(freqs - edges[i])))])
    tilt = np.linspace(0.0, 0.22, BANDS)  # compensa que los agudos tienen menos energía
    buf = np.zeros(n, dtype=np.float32)

    while not _stop.is_set():
        try:
            with _lock:
                enabled = _cfg["visualizer"]
            if not enabled:
                _audio_ok = False
                _stop.wait(0.5)
                continue
            spk = sc.default_speaker()
            mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
            with mic.recorder(samplerate=rate, channels=2, blocksize=1024) as rec:
                _audio_ok = True
                while not _stop.is_set():
                    with _lock:
                        if not _cfg["visualizer"]:
                            break
                    data = rec.record(numframes=1024)
                    mono = data.mean(axis=1).astype(np.float32)
                    buf = np.concatenate((buf[len(mono):], mono))
                    mag = np.abs(np.fft.rfft(buf * win)) / (n / 4)
                    out = []
                    for i in range(BANDS):
                        m = float(mag[idx[i]].max())
                        db = 20.0 * np.log10(m + 1e-9)
                        v = (db + 72.0) / 52.0 + tilt[i]
                        out.append(round(min(1.0, max(0.0, v)), 3))
                    with _lock:
                        _bars = out
        except Exception:
            _audio_ok = False
            with _lock:
                _bars = [0.0] * BANDS
            _stop.wait(2.0)
    _audio_ok = False


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
const N = 48;
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

async function pollBars(){
  if (busy) return; busy = true;
  try{
    const s = await (await fetch('/spectrum.json', {cache: 'no-store'})).json();
    target = s.bars; real = s.real;
  }catch(e){} finally { busy = false; }
}
setInterval(pollBars, 33);

function simulate(t){           // animacion de reserva si no hay audio real
  const out = [];
  for (let i=0;i<N;i++){
    const f = i/N;
    const v = .5 + .5*Math.sin(t*1.7 + i*.55) * Math.sin(t*.9 + i*.21);
    out.push(playing ? Math.max(0, (1-f*.75) * (.25 + .55*v) * (.8 + .2*Math.sin(t*3.1))) : 0);
  }
  return out;
}

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
  const src = real ? target : simulate(now/1000);
  for (let i=0;i<N;i++){
    const t = src[i] || 0;
    const k = t > cur[i] ? 1-Math.exp(-dt*22) : 1-Math.exp(-dt*5.5);   // sube rapido, cae suave
    cur[i] += (t - cur[i]) * k;
  }
  // suavizado entre bandas vecinas
  const sm = cur.map((v,i) => (cur[Math.max(0,i-1)] + 2*v + cur[Math.min(N-1,i+1)]) / 4);
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
                with _lock:
                    payload = {"bars": list(_bars), "real": bool(_audio_ok and _cfg["visualizer"])}
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
    _httpd_port = port
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()


def _stop_server():
    global _httpd, _httpd_port
    if _httpd is not None:
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
        "de Windows, igual que MusicPresence."
    )


def script_defaults(settings):
    for key, value in _cfg.items():
        if isinstance(value, bool):
            obs.obs_data_set_default_bool(settings, key, value)
        elif isinstance(value, int):
            obs.obs_data_set_default_int(settings, key, value)
        else:
            obs.obs_data_set_default_string(settings, key, value)


def script_properties():
    props = obs.obs_properties_create()

    obs.obs_properties_add_int(props, "port", "Puerto del overlay", 1024, 65535, 1)
    obs.obs_properties_add_bool(props, "hide_paused", "Ocultar overlay en pausa")
    obs.obs_properties_add_bool(props, "visualizer", "Visualizador de audio (waveform real)")

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
    global _last_applied
    with _lock:
        for key, value in _cfg.items():
            if isinstance(value, bool):
                _cfg[key] = obs.obs_data_get_bool(settings, key)
            elif isinstance(value, int):
                _cfg[key] = obs.obs_data_get_int(settings, key)
            else:
                _cfg[key] = obs.obs_data_get_string(settings, key)
        port = _cfg["port"]
        # forzar que se vuelva a leer la carátula con el nuevo filtro
        _state.update(key=None, cover=None, tries=0)
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
