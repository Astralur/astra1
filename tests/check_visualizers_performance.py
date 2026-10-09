"""Medición opcional de los 38 presets con señal continua y animación real."""
import json
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_audio import load_script
from test_visualizers import load_visualizers

musicbee, visualizers = load_script(), load_visualizers()
musicbee._start_server(0)
visualizers._cfg['musicbee_port'] = musicbee._httpd_port
visualizers._cfg['link_colors'] = True
visualizers._start_server(0)
stop = threading.Event()
processor = musicbee._SpectrumProcessor(np, rate=48000)

def feed():
    phase, deadline = 0, time.monotonic()
    while not stop.is_set():
        t = (np.arange(1024) + phase) / 48000
        signal = (.08 * np.sin(2*np.pi*110*t) + .035*np.sin(2*np.pi*1200*t)) * (.75+.25*np.sin(5*t))
        bars = processor.process(np.column_stack([signal, signal]).astype(np.float32))
        musicbee._publish_audio(bars, True, 'MusicBee fixture', wave=processor.wave, level=processor.level)
        phase += 1024
        deadline += 1024 / 48000
        stop.wait(max(0, deadline-time.monotonic()))

feeder = threading.Thread(target=feed, daemon=True)
worker = threading.Thread(target=visualizers._audio_worker, daemon=True)
feeder.start(); worker.start()
base = f'http://127.0.0.1:{visualizers._httpd_port}'
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH'), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width':900, 'height':300})
        errors=[]
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base)
        page.wait_for_function('packet.ready && main.level>.1')
        page.evaluate('''()=>{
          window.measuredFrames=[];window.drawCosts=[];
          const draw=main.draw.bind(main);
          main.draw=(...args)=>{const start=performance.now();draw(...args);if(main.visible){measuredFrames.push(start);drawCosts.push(performance.now()-start);}};
        }''')
        def measure():
            return page.evaluate('''async()=>{
              measuredFrames=[];drawCosts=[];
              await new Promise(resolve=>setTimeout(resolve,800));
              const times=measuredFrames,sorted=drawCosts.slice().sort((a,b)=>a-b);
              return {fps:(times.length-1)*1000/(times.at(-1)-times[0]),
                      draw_p95_ms:sorted[Math.floor(sorted.length*.95)]};
            }''')
        results=[]
        for style in visualizers.PRESETS:
            with visualizers._audio_updated:
                visualizers._cfg['style']=style
                visualizers._audio_seq+=1
                visualizers._audio_updated.notify_all()
            page.wait_for_function('(style)=>main.style===style',arg=style)
            page.wait_for_timeout(100)
            result={'style':style, **measure()}
            results.append(result)
            print(json.dumps(result),flush=True)
        page.set_viewport_size({'width':1280,'height':900})
        page.goto(base+'/compare')
        page.wait_for_function('packet.ready && main.visible')
        page.evaluate('''()=>{window.measuredFrames=[];window.drawCosts=[];const draw=main.draw.bind(main);main.draw=(...args)=>{const start=performance.now();draw(...args);measuredFrames.push(start);drawCosts.push(performance.now()-start);};}''')
        gallery=measure()
        print(json.dumps({'gallery':gallery}),flush=True)
        assert all(result['fps']>=55 for result in results), results
        assert gallery['fps']>=55, gallery
        assert not errors, errors
        print('PASS: 38 presets y galería cerca de 60 FPS en este Chromium a 900×300 / 1280×900.')
        browser.close()
finally:
    stop.set();visualizers._stop.set()
    feeder.join(2);worker.join(3)
    visualizers._stop_server();musicbee._stop_server()
