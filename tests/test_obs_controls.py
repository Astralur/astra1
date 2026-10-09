"""Selección real de presets, persistencia y FPS de las fuentes propias de OBS."""
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from test_audio import load_script
from test_visualizers import load_visualizers


class FakeObs:
    def __init__(self, sources=()):
        self.sources = list(sources)
        self.updates = []
        self.releases = []

    def __getattr__(self, name):
        if name.startswith('OBS_'):
            return 1
        if name.startswith('obs_properties_add_'):
            return lambda props, key, *args: props.setdefault(key, {'options': []})
        raise AttributeError(name)

    def obs_properties_create(self): return {}
    def obs_properties_get(self, props, key): return props[key]
    def obs_property_list_clear(self, prop): prop['options'].clear()
    def obs_property_list_add_string(self, prop, label, value): prop['options'].append((label, value))
    obs_property_list_add_int = obs_property_list_add_string
    def obs_property_set_visible(self, prop, visible): prop['visible'] = visible
    def obs_property_set_modified_callback(self, prop, callback): prop['callback'] = callback
    def obs_enum_sources(self): return self.sources
    def source_list_release(self, sources): self.releases.append('sources')
    def obs_source_get_unversioned_id(self, source): return source['id']
    def obs_source_get_output_flags(self, source): return 0
    def obs_source_get_settings(self, source): return source['settings']
    def obs_source_update(self, source, settings): self.updates.append(source)
    def obs_data_release(self, settings): self.releases.append(settings)
    def obs_data_get_string(self, settings, key): return settings.get(key, '')
    def obs_data_get_bool(self, settings, key): return bool(settings.get(key, False))
    def obs_data_get_int(self, settings, key): return settings.get(key, 0)
    def obs_data_set_string(self, settings, key, value): settings[key] = value
    obs_data_set_bool = obs_data_set_string
    obs_data_set_int = obs_data_set_string


class ObsControlsTests(unittest.TestCase):
    def test_all_38_presets_are_selectable_in_actual_obs_properties(self):
        module = load_visualizers()
        with patch.object(module, 'obs', FakeObs()):
            props = module.script_properties()
        self.assertEqual(len(props['style']['options']), 38)
        self.assertEqual(dict((value, label) for label, value in props['style']['options']), module.PRESETS)

    def test_fps_fix_applies_only_to_own_overlay_sources_and_only_once(self):
        for module in (load_script(), load_visualizers()):
            port = module._cfg['port']
            def source(url, **extras):
                return {'id': 'browser_source', 'settings': {'url': url, 'fps': 30, 'fps_custom': False, **extras}}
            root = source(f'http://localhost:{port}/')
            preset = source(f'http://127.0.0.1:{port}/?style=ring_spiral')
            alias = source(f'http://localhost:{port}/overlay')
            excluded = [source(f'http://localhost:{port}/compare'),
                        source(f'http://localhost:{port+1}/'),
                        source(f'http://example.com:{port}/'), source('http://['),
                        {'id': 'text_gdiplus', 'settings': {}}]
            obs = FakeObs([root, preset, alias, *excluded])
            with self.subTest(module=module.__name__), patch.object(module, 'obs', obs):
                module._ensure_browser_fps()
                self.assertEqual(obs.updates, [root, preset, alias])
                for item in obs.updates:
                    self.assertEqual(item['settings']['fps'], 60)
                    self.assertTrue(item['settings']['fps_custom'])
                module._ensure_browser_fps()
                self.assertEqual(len(obs.updates), 3)
                self.assertEqual(obs.releases.count('sources'), 2)

    def test_every_preset_can_be_applied_over_http_and_saved_in_obs(self):
        module = load_visualizers()
        module._start_server(0)
        base = f'http://127.0.0.1:{module._httpd_port}'
        settings = {}
        module._settings_ref = settings
        module._last_browser_scan = float('inf')
        try:
            with patch.object(module, 'obs', FakeObs()):
                for style in module.PRESETS:
                    request = Request(base + '/preset', data=json.dumps({'style': style}).encode(),
                                      headers={'Content-Type': 'application/json', 'Origin': base})
                    with urlopen(request) as response:
                        self.assertEqual(json.load(response)['style'], style)
                    self.assertEqual(module._snapshot()['config']['style'], style)
                    module._obs_tick()
                    self.assertEqual(settings['style'], style)
                    saved = {}
                    module.script_save(saved)
                    self.assertEqual(saved['style'], style)
                for body, headers, status in [({'style': 'missing'}, {'Content-Type': 'application/json'}, 400),
                                              ([], {'Content-Type': 'application/json'}, 400),
                                              ({'style': 'ring'}, {'Content-Type': 'text/plain'}, 415),
                                              ({'style': 'ring'}, {'Content-Type': 'application/json', 'Origin': 'http://example.com'}, 403)]:
                    before = module._cfg['style']
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(Request(base + '/preset', data=json.dumps(body).encode(), headers=headers))
                    self.assertEqual(raised.exception.code, status)
                    raised.exception.close()
                    self.assertEqual(module._cfg['style'], before)
        finally:
            module._settings_ref = None
            module._stop_server()

    def test_color_option_is_persisted_and_keeps_manual_colors(self):
        module = load_visualizers()
        manual = {key: module._cfg[key] for key in ('color_a', 'color_b', 'background_color')}
        settings = {}
        module._settings_ref = settings
        module._last_browser_scan = float('inf')
        with patch.object(module, 'obs', FakeObs()):
            for enabled in (True, False):
                module._set_color_link(enabled)
                module._obs_tick()
                self.assertEqual(settings['link_colors'], enabled)
                saved = {}
                module.script_save(saved)
                self.assertEqual(saved['link_colors'], enabled)
                self.assertEqual({key: module._cfg[key] for key in manual}, manual)
            props = module.script_properties()
            module._mode_visibility(props, 'obs', True)
            self.assertTrue(props['musicbee_port']['visible'])
            module._mode_visibility(props, 'obs', False)
            self.assertFalse(props['musicbee_port']['visible'])


if __name__ == '__main__':
    unittest.main()
