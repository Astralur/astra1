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
            self.assertEqual(len(processor.wave), 256)
            self.assertAlmostEqual(processor.level, .48, places=4)
            self.assertGreater(max(processor.wave), .15)
            self.assertLess(min(processor.wave), -.15)
            crossings = sum(a <= 0 < b for a, b in zip(processor.wave, processor.wave[1:]))
            self.assertGreaterEqual(crossings, 15)
            self.assertLessEqual(crossings, 17)
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
        self.assertEqual(processor.wave, [0.0] * 256)
        self.assertEqual(processor.level, 0)

    def test_musicbee_and_visualizers_share_actual_http_audio_data(self):
        musicbee = load_script()
        musicbee._publish_audio([.4] * 48, True, 'MusicBee', wave=[.12] * 256, level=.48)
        server = musicbee.ThreadingHTTPServer(('127.0.0.1', 0), musicbee._Handler)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
        try:
            self.module._read_musicbee(server.server_port)
            snapshot = self.module._snapshot()
            self.assertTrue(snapshot['ready'])
            self.assertEqual(snapshot['source'], 'MusicBee')
            self.assertEqual(snapshot['bands'], [.4] * 48)
            self.assertEqual(snapshot['wave'], [.12] * 256)
            self.assertEqual(snapshot['level'], .48)
            musicbee._publish_audio([0] * 48, error='Sin fuente de audio')
            self.module._read_musicbee(server.server_port)
            snapshot = self.module._snapshot()
            self.assertFalse(snapshot['ready'])
            self.assertEqual(snapshot['bands'], [0] * 64)
            self.assertEqual(snapshot['wave'], [0] * 256)
            self.assertEqual(snapshot['error'], 'Sin fuente de audio')
        finally:
            server.shutdown()
            server.server_close()

    def test_stale_or_disabled_audio_is_not_kept_alive(self):
        self.module._publish_audio([.8] * 64, True, 'MusicBee', wave=[.3] * 256, level=.8)
        self.module._audio['stamp'] = time.monotonic() - 1
        self.assertFalse(self.module._snapshot()['ready'])
        self.module._publish_audio([.8] * 64, True, 'MusicBee', wave=[.3] * 256, level=.8)
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
        finally:
            self.module._stop_server()


if __name__ == '__main__':
    unittest.main()
