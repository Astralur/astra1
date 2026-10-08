"""Pruebas del DSP y del aislamiento de entrada, sin requerir Windows/OBS."""
import ctypes
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


def load_script():
    path = Path(__file__).resolve().parents[1] / 'musicbee_nowplaying.py'
    spec = importlib.util.spec_from_file_location('musicbee_overlay_test', path)
    module = importlib.util.module_from_spec(spec)
    obs = types.SimpleNamespace(script_log=lambda *args: None, LOG_INFO=1, LOG_WARNING=2)
    with patch.dict(sys.modules, {'obspython': obs}):
        spec.loader.exec_module(module)
    return module


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script()
        t = np.arange(1024) / 44100
        mono = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
        self.tone = np.column_stack([mono, mono])

    def test_different_input_volumes_produce_same_level_and_spectrum(self):
        outputs = []
        for level in [.005, .05, .5]:
            processor = self.module._SpectrumProcessor(np)
            data = self.tone * level
            untouched = data.copy()
            for _ in range(8):
                bars = processor.process(data)
            self.assertAlmostEqual(float(np.sqrt(np.mean(processor.buf ** 2))), .12, places=5)
            np.testing.assert_array_equal(data, untouched)
            outputs.append(bars)
        np.testing.assert_allclose(outputs[0], outputs[1], atol=.002)
        np.testing.assert_allclose(outputs[1], outputs[2], atol=.002)
        self.assertGreater(max(outputs[0]), .5)

    def test_gain_recovers_after_volume_change(self):
        processor = self.module._SpectrumProcessor(np)
        for _ in range(8):
            processor.process(self.tone * .5)
        for _ in range(150):
            processor.process(self.tone * .005)
        self.assertAlmostEqual(float(np.sqrt(np.mean(processor.buf ** 2))), .12, delta=.001)
        for _ in range(20):
            processor.process(self.tone * .5)
        self.assertAlmostEqual(float(np.sqrt(np.mean(processor.buf ** 2))), .12, delta=.001)
        self.assertLessEqual(float(np.abs(processor.buf).max()), .98)

    def test_silence_and_noise_floor_do_not_create_waves(self):
        processor = self.module._SpectrumProcessor(np)
        processor.process(self.tone * .5)
        for data in [np.zeros_like(self.tone), self.tone * .00001]:
            self.assertEqual(processor.process(data), [0.0] * self.module.BANDS)
            self.assertEqual(float(np.abs(processor.buf).max()), 0.0)
        processor.process(self.tone * .005)
        self.assertAlmostEqual(processor.gain, .12 / float(np.sqrt(np.mean((self.tone * .005) ** 2))), places=4)

    def test_transient_peaks_stay_below_full_scale(self):
        processor = self.module._SpectrumProcessor(np)
        processor.process(self.tone * .005)
        impulse = np.zeros_like(self.tone)
        impulse[500] = 1
        processor.process(impulse)
        self.assertLessEqual(float(np.abs(processor.buf).max()), .980001)

    def test_opposite_stereo_polarity_does_not_cancel_music(self):
        normal = self.module._SpectrumProcessor(np)
        opposite = self.module._SpectrumProcessor(np)
        anti = self.tone.copy()
        anti[:, 1] *= -1
        for _ in range(8):
            a = normal.process(self.tone * .1)
            b = opposite.process(anti * .1)
        np.testing.assert_allclose(a, b, atol=.002)
        self.assertGreater(max(b), .5)

    def fake_api(self, channels=2, rate=48000, names=None):
        module = self.module
        names = names or {'MusicBee': 1, 'Microphone': 2}

        class FakeApi:
            def __init__(self):
                self.callbacks = {}
                self.events = []
                self.flags = {source: 2 for source in names.values()}
                self.removed = set()
                self.on_register = None

            def audio_info(self):
                return rate, channels

            def obs_get_source_by_name(self, name):
                return names.get(name.decode('utf-8'))

            def obs_source_get_output_flags(self, source):
                return self.flags[source]

            def obs_source_add_audio_capture_callback(self, source, callback, param):
                self.events.append(('add', source))
                self.callbacks[source] = callback
                if self.on_register:
                    self.on_register(source)

            def obs_source_remove_audio_capture_callback(self, source, callback, param):
                assert self.callbacks[source] is callback
                self.events.append(('remove', source))
                self.callbacks.pop(source)

            def obs_source_release(self, source):
                self.events.append(('release', source))

            def obs_source_removed(self, source):
                return source in self.removed

            def emit(self, source, samples, muted=False):
                data = module._ObsAudioData()
                buffers = [np.ascontiguousarray(samples[:, i], dtype=np.float32)
                           for i in range(samples.shape[1])]
                for i, buffer in enumerate(buffers):
                    data.data[i] = buffer.ctypes.data
                data.frames = len(samples)
                if source in self.callbacks:
                    self.callbacks[source](None, source, ctypes.byref(data), muted)
                return buffers

        return FakeApi()

    def test_tap_copies_only_the_selected_source_before_obs_reuses_memory(self):
        module = self.module
        api = self.fake_api()
        tap = module._ObsAudioTap(api, 'MusicBee')
        api.emit(2, self.tone * .9)  # Otra fuente no está suscrita.
        self.assertTrue(tap.packets.empty())
        expected = self.tone * .05
        buffers = api.emit(1, expected)
        for buffer in buffers:
            buffer.fill(0)  # OBS ya puede reutilizar los buffers originales.
        np.testing.assert_array_equal(tap.read(np), expected)
        tap.close()
        self.assertEqual(api.events, [('add', 1), ('remove', 1), ('release', 1)])
        tap.close()  # Cierre repetido sin liberar dos veces.
        self.assertEqual(len(api.events), 3)

    def test_muted_source_returns_silence(self):
        api = self.fake_api()
        tap = self.module._ObsAudioTap(api, 'MusicBee')
        api.emit(1, self.tone * .5, muted=True)
        self.assertIsNone(tap.read(np))
        tap.close()

    def test_musicbee_can_read_audio_even_when_source_is_muted(self):
        api = self.fake_api()
        tap = self.module._ObsAudioTap(api, 'MusicBee', capture_muted=True)
        api.emit(1, self.tone * .05, muted=True)
        np.testing.assert_array_equal(tap.read(np), self.tone * .05)
        tap.close()
        self.assertTrue(self.module._cfg['capture_muted'])

    def test_source_without_audio_and_missing_source_have_no_fallback(self):
        api = self.fake_api()
        api.flags[1] = 0
        for name in ['Missing source', 'MusicBee']:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.module._ObsAudioTap(api, name)
        self.assertEqual(api.events, [('release', 1)])
        self.assertFalse(api.callbacks)

    def test_removed_source_is_released_without_subscribing(self):
        api = self.fake_api()
        api.removed.add(1)
        with self.assertRaises(RuntimeError):
            self.module._ObsAudioTap(api, 'MusicBee')
        self.assertEqual(api.events, [('release', 1)])
        self.assertFalse(api.callbacks)

    def test_queue_keeps_latest_audio_and_stays_bounded(self):
        api = self.fake_api()
        tap = self.module._ObsAudioTap(api, 'MusicBee')
        for i in range(20):
            api.emit(1, self.tone * (i / 100))
        self.assertLessEqual(tap.packets.qsize(), 4)
        np.testing.assert_array_equal(tap.read(np), self.tone * .19)
        tap.close()

    def test_mono_and_surround_sources_can_be_normalized(self):
        for channels in [1, 6, 8]:
            with self.subTest(channels=channels):
                api = self.fake_api(channels=channels)
                tap = self.module._ObsAudioTap(api, 'MusicBee')
                samples = np.tile(self.tone[:, :1], (1, channels)) * .05
                api.emit(1, samples)
                data = tap.read(np)
                self.assertEqual(data.shape, (1024, channels))
                processor = self.module._SpectrumProcessor(np, rate=tap.rate)
                self.assertGreater(max(processor.process(data)), .5)
                tap.close()

    def test_worker_switches_source_and_unregisters_each_callback(self):
        module = self.module
        module._cfg['audio_source'] = 'MusicBee'
        api = self.fake_api()
        api.on_register = lambda source: api.emit(source, self.tone * .05)
        publications = []

        def publish(bars, ready=False, source='', error=None, **extras):
            publications.append((list(bars), ready, source, error))
            if ready:
                if source == 'MusicBee':
                    module._cfg['audio_source'] = 'Microphone'
                else:
                    module._stop.set()

        with patch.object(module, '_ObsAudioApi', return_value=api), patch.object(module, '_publish_audio', publish):
            module._audio_worker()
        active = [p for p in publications if p[1]]
        self.assertEqual([p[2] for p in active], ['MusicBee', 'Microphone'])
        self.assertTrue(all(max(p[0]) > .5 for p in active))
        self.assertEqual(api.events, [('add', 1), ('remove', 1), ('release', 1),
                                      ('add', 2), ('remove', 2), ('release', 2)])
        self.assertFalse(publications[-1][1])
        self.assertFalse(api.callbacks)


if __name__ == '__main__':
    unittest.main()
