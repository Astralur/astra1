"""Comprobación opcional en Chromium: presets, controles y audio compartido por SSE."""
import hashlib
import os
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_audio import load_script
from test_visualizers import load_visualizers
from playwright.sync_api import sync_playwright
import numpy as np

musicbee = load_script()
visualizers = load_visualizers()
old_server = musicbee.ThreadingHTTPServer(('127.0.0.1', 0), musicbee._Handler)
threading.Thread(target=old_server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
visualizers._cfg['musicbee_port'] = old_server.server_port
visualizers._start_server(0)
fixture_stop = threading.Event()
fixture = {'signal': True}

def feed():
    while not fixture_stop.is_set():
        t = time.monotonic()
        if fixture['signal']:
            bands = [max(0, (.35 + .3 * np.sin(t * 2 + i * .33)) * (1 - i / 180)) for i in range(128)]
            wave = [.2 * np.sin(i * .2) + .03 * np.sin(i * .5) for i in range(1024)]
            level = .48
        else:
            bands, wave, level = [0] * 128, [0] * 1024, 0
        musicbee._publish_audio(bands, True, 'MusicBee OBS', wave=wave, level=level)
        fixture_stop.wait(.02)

feeder = threading.Thread(target=feed, daemon=True)
feeder.start()
visualizers._stop.clear()
worker = threading.Thread(target=visualizers._audio_worker, daemon=True)
worker.start()
base = f'http://127.0.0.1:{visualizers._httpd_port}'
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH'), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width':1280,'height':900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(base + '/compare')
        page.wait_for_function("document.getElementById('signal').textContent.includes('MusicBee OBS')")
        page.wait_for_timeout(800)
        assert page.locator('.preset').count() == 38
        records = page.evaluate("""()=>{const cv=document.createElement('canvas');cv.style.cssText='position:fixed;left:-3000px;width:900px;height:300px';document.body.append(cv);const results=Object.keys(STYLES).map(style=>{const r=new Visualizer(cv,style);r.visible=true;r.draw(packet,.05,{...config,attack_ms:0,release_ms:0});r.observer.disconnect();r.visibility.disconnect();return {style,image:cv.toDataURL(),energy:Math.max(...r.v),pixels:Array.from(r.g.getImageData(0,0,cv.width,cv.height).data).filter((v,i)=>i%4===3&&v>0).length};});cv.remove();return results;}""")
        assert len({hashlib.sha256(r['image'].encode()).hexdigest() for r in records}) == 38
        assert all(r['energy'] > .1 and r['pixels'] > 100 for r in records), [(r['style'],r['pixels']) for r in records]
        assert page.evaluate('renderers.slice(1).some(r=>!r.visible)')
        page.get_by_role('button',name='Ondas',exact=True).click()
        assert page.locator('.preset:visible').count() == 12
        page.locator('#filters button').filter(has_text='Partículas').click()
        assert page.locator('.preset:visible').count() == 3
        page.locator('#filters button').filter(has_text='Todos').click()
        for style in visualizers.PRESETS:
            page.locator(f'.preset[data-style={style}]').click()
            assert page.evaluate('main.style') == style
            assert page.locator('.preset[aria-pressed=true]').count() == 1
        page.locator('.preset[data-style=ribbon]').click()
        page.evaluate('window.scrollTo(0,0)')
        page.wait_for_timeout(300)
        fixture['signal'] = False
        page.wait_for_function('renderers.filter(r=>r.visible).every(r=>Math.max(...r.v)<.001)',timeout=5000)
        assert page.evaluate('renderers.filter(r=>r.visible).every(r=>r.level<.001)')
        page.locator('#demo').check()
        page.wait_for_function("document.getElementById('signal').textContent.startsWith('Demostración')")
        page.wait_for_function('main.level>.2')
        assert visualizers._snapshot()['level'] == 0
        page.locator('#demo').uncheck()
        page.wait_for_function('main.level<.001',timeout=5000)
        fixture['signal'] = True
        page.goto(base)
        page.wait_for_function('packet.ready && main.level>.1')
        controls=page.evaluate("""()=>{const cv=document.createElement('canvas');cv.style.cssText='position:fixed;left:-3000px;width:900px;height:300px';document.body.append(cv);const r=new Visualizer(cv,'bars');const bright={ready:true,bands:new Array(128).fill(1),wave:new Array(1024).fill(.2),level:1},quiet={...bright,ready:false};const cfg={...config,smoothing:0};const rise=ms=>{r.v.fill(0);r.level=0;r.draw(bright,.02,{...cfg,attack_ms:ms});return r.level;};const fall=ms=>{r.v.fill(1);r.level=1;r.draw(quiet,.02,{...cfg,release_ms:ms});return r.level;};const answer={fastRise:rise(10),slowRise:rise(500),immediateRise:rise(0),fastFall:fall(20),slowFall:fall(500),immediateFall:fall(0)};const spikes={...bright,bands:Array.from({length:128},(_,i)=>i%2)};r.v.fill(0);r.draw(spikes,.02,{...cfg,attack_ms:0,density:128});const raw=cv.toDataURL(),before=[...r.v];r.draw(spikes,.02,{...cfg,attack_ms:0,density:128,smoothing:1});answer.formChanged=raw!==cv.toDataURL();answer.sameResponse=JSON.stringify(before)===JSON.stringify(r.v);r.style='scope';let segments=0;const line=r.g.lineTo.bind(r.g);r.g.lineTo=(...args)=>{segments++;line(...args);};r.draw(packet,.02,cfg);answer.waveSegments=segments;r.observer.disconnect();cv.remove();return answer;}""")
        assert controls['fastRise'] > controls['slowRise']*10, controls
        assert controls['immediateRise'] == 1 and controls['immediateFall'] == 0, controls
        assert controls['fastFall'] < controls['slowFall']*.5, controls
        assert controls['formChanged'] and controls['sameResponse'], controls
        assert controls['waveSegments'] == 1023, controls
        assert page.evaluate('document.body.style.background') == 'transparent'
        for style in visualizers.PRESETS:
            with visualizers._lock:
                visualizers._cfg['style'] = style
            page.wait_for_function('(style)=>main.style===style',arg=style)
        with visualizers._lock:
            visualizers._cfg.update(style='scope',density=96,thickness=8,background=True)
        page.wait_for_function("getComputedStyle(document.body).backgroundColor==='rgb(8, 16, 24)'")
        page.set_viewport_size({'width':320,'height':240})
        page.wait_for_timeout(200)
        assert page.evaluate('main.w===320 && main.h===240')
        with visualizers._lock:
            visualizers._cfg.update(background=False,density=16)
        page.goto(base + '/?style=ring')
        page.wait_for_function("main.style==='ring'")
        with visualizers._lock:
            visualizers._cfg['style'] = 'bars'
        page.wait_for_timeout(150)
        assert page.evaluate("main.style==='ring'")
        with visualizers._lock:
            visualizers._cfg['visualizer'] = False
        page.wait_for_function('!packet.ready && main.level<.001 && Math.max(...main.v)<.001',timeout=5000)
        assert page.evaluate('Math.max(...main.v)<.001')
        assert not errors, errors
        browser.close()
    print('PASS: 38 distinct rendered styles; independent attack/release and spatial smoothing; 1024 waveform points; gallery category filters and offscreen rendering; shared MusicBee audio through both actual HTTP servers; gallery selection; silence; gallery-only demo; live style/config changes; transparency/background; density/thickness; responsive canvas; fixed-style URL; disable; no JavaScript errors.')
finally:
    visualizers._stop.set()
    worker.join(3)
    fixture_stop.set()
    feeder.join(1)
    visualizers._stop_server()
    old_server.shutdown()
    old_server.server_close()
