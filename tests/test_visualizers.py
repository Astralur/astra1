import importlib.util
import json
import math
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from urllib.request import urlopen

import numpy as np
from unittest.mock import patch
from test_audio import load_script


def load_visualizers():
    path = Path(__file__).resolve().parents[1] / 'obs_visualizers.py'
    spec = importlib.util.spec_from_file_location('obs_visualizers_test', path)
    module = importlib.util.module_from_spec(spec)
    obs = types.SimpleNamespace(script_log=lambda *args: None, LOG_INFO=1, LOG_WARNING=2)
    with patch.dict(sys.modules, {'obspython': obs}):
        spec.loader.exec_module(module)
    return module


class VisualizerTests(unittest.TestCase):
    def setUp(self):
        self.module = load_visualizers()

    def test_real_waveform_is_normalized_and_preserves_frequency(self):
        t = np.arange(1024) / 48000
        signal = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
        values = []
        for volume in [.005, .5]:
            processor = self.module._SpectrumProcessor(np, rate=48000)
            for _ in range(8):
                processor.process(np.column_stack([signal, -signal]) * volume)
            self.assertEqual(len(processor.wave), self.module.WAVE_POINTS)
            self.assertAlmostEqual(processor.level, .48, places=4)
            self.assertGreater(max(processor.wave), .15)
            self.assertLess(min(processor.wave), -.15)
            crossings = sum(a <= 0 < b for a, b in zip(processor.wave, processor.wave[1:]))
            self.assertGreaterEqual(crossings, 20)
            self.assertLessEqual(crossings, 22)
            values.append(processor.wave)
        np.testing.assert_allclose(values[0], values[1], atol=.0002)

    def test_normalization_can_be_disabled_in_independent_mode(self):
        processor = self.module._SpectrumProcessor(np)
        data = np.full((1024, 2), .01, dtype=np.float32)
        processor.process(data, normalize=False)
        self.assertAlmostEqual(processor.level, .04, places=5)
        processor.process(data, normalize=True)
        self.assertGreater(processor.level, .04)
        processor.process(np.zeros_like(data))
        self.assertEqual(processor.wave, [0.0] * self.module.WAVE_POINTS)
        self.assertEqual(processor.level, 0)

    def test_musicbee_and_visualizers_share_actual_http_audio_data(self):
        musicbee = load_script()
        musicbee._publish_audio([.4] * self.module.BANDS, True, 'MusicBee', wave=[.12] * self.module.WAVE_POINTS, level=.48)
        server = musicbee.ThreadingHTTPServer(('127.0.0.1', 0), musicbee._Handler)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
        try:
            self.module._read_musicbee(server.server_port)
            snapshot = self.module._snapshot()
            self.assertTrue(snapshot['ready'])
            self.assertEqual(snapshot['source'], 'MusicBee')
            self.assertEqual(snapshot['bands'], [.4] * self.module.BANDS)
            self.assertEqual(snapshot['wave'], [.12] * self.module.WAVE_POINTS)
            self.assertEqual(snapshot['level'], .48)
            musicbee._publish_audio([0] * self.module.BANDS, error='Sin fuente de audio')
            self.module._read_musicbee(server.server_port)
            snapshot = self.module._snapshot()
            self.assertFalse(snapshot['ready'])
            self.assertEqual(snapshot['bands'], [0] * self.module.BANDS)
            self.assertEqual(snapshot['wave'], [0] * self.module.WAVE_POINTS)
            self.assertEqual(snapshot['error'], 'Sin fuente de audio')
        finally:
            server.shutdown()
            server.server_close()

    def test_stale_or_disabled_audio_is_not_kept_alive(self):
        self.module._publish_audio([.8] * self.module.BANDS, True, 'MusicBee', wave=[.3] * self.module.WAVE_POINTS, level=.8)
        self.module._audio['stamp'] = time.monotonic() - 1
        self.assertFalse(self.module._snapshot()['ready'])
        self.module._publish_audio([.8] * self.module.BANDS, True, 'MusicBee', wave=[.3] * self.module.WAVE_POINTS, level=.8)
        self.module._cfg['visualizer'] = False
        result = self.module._snapshot()
        self.assertFalse(result['ready'])
        self.assertEqual(result['level'], 0)

    def test_web_routes_and_live_config(self):
        self.module._start_server(0)
        base = f'http://127.0.0.1:{self.module._httpd_port}'
        try:
            for route in ['/', '/overlay', '/compare']:
                with urlopen(base + route) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn('const STYLES=', response.read().decode())
            self.module._cfg['style'] = 'ring'
            with urlopen(base + '/state.json') as response:
                result = json.load(response)
            self.assertEqual(result['config']['style'], 'ring')
            self.assertEqual(result['config']['color_a'], '#56e1ff')
            self.assertEqual(result['config']['color_b'], '#ad72ff')
            self.assertFalse(result['config']['background'])
            self.assertEqual(result['config']['attack_ms'], 18)
            self.assertEqual(result['config']['release_ms'], 140)
        finally:
            self.module._stop_server()

    def test_musicbee_stream_forwards_audio_updates_and_stops_cleanly(self):
        musicbee = load_script()
        musicbee._start_server(0)
        self.module._cfg['musicbee_port'] = musicbee._httpd_port
        received = threading.Event()
        original = self.module._publish_audio

        def publish(*args, **kwargs):
            original(*args, **kwargs)
            if kwargs.get('level') == .7:
                received.set()

        errors = []
        def consume():
            try:
                self.module._stream_musicbee(musicbee._httpd_port)
            except Exception as e:
                errors.append(str(e))

        with patch.object(self.module, '_publish_audio', publish):
            thread = threading.Thread(target=consume, daemon=True)
            thread.start()
            try:
                musicbee._publish_audio([.9] * musicbee.BANDS, True, 'MusicBee',
                                        wave=[.23] * musicbee.WAVE_POINTS, level=.7,
                                        analysis={'sample_rate': 48000, 'fft_size': 8192})
                self.assertTrue(received.wait(2), errors)
                snapshot = self.module._snapshot()
                self.assertTrue(snapshot['ready'])
                self.assertEqual(snapshot['wave'], [.23] * 1024)
                self.assertEqual(snapshot['analysis']['fft_size'], 8192)
                self.module._stop.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors)
            finally:
                self.module._stop.set()
                thread.join(2)
                musicbee._stop_server()

    def test_browser_event_stream_updates_audio_and_response_controls(self):
        self.module._start_server(0)
        base = f'http://127.0.0.1:{self.module._httpd_port}'
        def event(response):
            while True:
                line = response.readline()
                if line.startswith(b'data: '):
                    return json.loads(line[6:])
                if not line:
                    self.fail('El flujo terminó antes de enviar el evento.')
        try:
            with urlopen(base + '/events', timeout=2) as response:
                self.assertEqual(response.headers.get_content_type(), 'text/event-stream')
                self.assertFalse(event(response)['ready'])
                self.module._cfg.update(attack_ms=0.0, release_ms=850.0, smoothing=.85)
                self.module._publish_audio([.5] * 128, True, 'MusicBee', wave=[.15] * 1024, level=.6)
                result = event(response)
                self.assertTrue(result['ready'])
                self.assertEqual(len(result['wave']), 1024)
                self.assertEqual(result['config']['attack_ms'], 0)
                self.assertEqual(result['config']['release_ms'], 850)
                self.assertEqual(result['config']['smoothing'], .85)
        finally:
            self.module._stop_server()

    def test_cover_proxy_checks_revision_and_supports_independent_audio(self):
        musicbee = load_script()
        musicbee._state.update(cover=b'cover fixture', mime='image/png', rev=4)
        musicbee._start_server(0)
        self.module._cfg.update(musicbee_port=musicbee._httpd_port, audio_mode='obs', link_colors=True)
        self.module._start_server(0)
        base = f'http://127.0.0.1:{self.module._httpd_port}'
        try:
            with urlopen(base + '/musicbee-theme.json') as response:
                self.assertEqual(json.load(response), {'rev': 4, 'has_cover': True})
            with urlopen(base + '/musicbee-cover?rev=4') as response:
                self.assertEqual(response.headers.get_content_type(), 'image/png')
                self.assertEqual(response.read(), b'cover fixture')
            musicbee._state.update(cover=b'new cover', rev=5)
            with urlopen(base + '/musicbee-cover?rev=5') as response:
                self.assertEqual(response.read(), b'new cover')
            with self.assertRaises(RuntimeError):
                self.module._musicbee_cover(3)
            musicbee._state.update(cover=None, rev=6)
            with urlopen(base + '/musicbee-theme.json') as response:
                self.assertEqual(json.load(response), {'rev': 6, 'has_cover': False})
        finally:
            self.module._stop_server()
            musicbee._stop_server()


if __name__ == '__main__':
    unittest.main()
