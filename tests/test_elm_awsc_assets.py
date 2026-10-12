"""Offline AWSC resource and QingLong subscription regression checks."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'wx-script' / 'elm_cookie_assets'
SPEC = importlib.util.spec_from_file_location('elm_assets_test', ROOT / 'wx-script' / 'elm_cookie.py')
ELM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ELM)
NODE = shutil.which('node')


class ResourceTests(unittest.TestCase):
    def test_resources_are_beside_the_business_script(self):
        self.assertEqual(Path(ELM.__file__).with_name('elm_cookie_assets'), ASSETS)
        for name in ('awsc_bridge.cjs', 'awsc_params.cjs', 'package.json', 'mor.v.js',
                     'plugin_/wxd46b6e9c9b775a56/fireyejs.min.js', '@babel/runtime/helpers/typeof.js'):
            self.assertTrue((ASSETS / name).is_file(), name)

    def test_commonjs_boundary_is_present(self):
        package = json.loads((ASSETS / 'package.json').read_text(encoding='utf-8'))
        self.assertEqual(package['type'], 'commonjs')

    def test_runtime_credentials_and_device_cache_are_not_bundled(self):
        forbidden = {'.protocol_device.json', 'algorithm_params_result.json'}
        self.assertFalse(any(p.name in forbidden for p in ASSETS.rglob('*')))

    def test_subscription_commands_sync_all_sources_as_dependencies(self):
        commands = re.findall(r'^ql repo .*$', (ROOT / 'README.md').read_text(encoding='utf-8'), re.M)
        sources = [p.relative_to(ROOT).as_posix() for p in ASSETS.rglob('*')
                   if p.is_file() and p.suffix in ('.js', '.cjs', '.json')]
        self.assertEqual(len(sources), 40)
        awsc_commands = [cmd for cmd in commands if 'elm_cookie_assets' in cmd]
        self.assertEqual(len(awsc_commands), 2)
        for command in awsc_commands:
            args = re.findall(r'"([^"]*)"', command)
            self.assertEqual(len(args), 6)
            whitelist, dependence = args[1], args[3]
            extensions = args[5].split()
            self.assertTrue(re.search(whitelist, 'wx-script/elm_cookie.py'))
            for source in sources:
                self.assertIn(source.rsplit('.', 1)[-1], extensions)
                self.assertTrue(re.search(dependence, source), source)
                self.assertFalse(re.search(whitelist, source), source)

    def test_disabled_awsc_does_not_spawn_node(self):
        with patch.dict(os.environ, {'elm_awsc_enabled': '0'}, clear=True), \
             patch.object(ELM.subprocess, 'run') as run:
            self.assertEqual(ELM.build_login_context(), ({}, ''))
            run.assert_not_called()

    def test_missing_node_keeps_base_login_fallback(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'elm_awsc_enabled': '1'}, clear=True), \
             patch.object(ELM.shutil, 'which', return_value=None), \
             contextlib.redirect_stdout(output):
            self.assertEqual(ELM.build_login_context(), ({}, ''))
        self.assertIn('AWSC 不可用', output.getvalue())

    @unittest.skipUnless(NODE, 'Node is required for the offline SDK test')
    def test_actual_bridge_loads_without_network_or_existing_device_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            assets = work / 'elm_cookie_assets'
            shutil.copytree(ASSETS, assets)
            (work / 'package.json').write_text('{"type":"module"}')
            # Blocks outbound sockets even if a future SDK ignores noNetwork.
            guard = work / 'block-network.cjs'
            guard.write_text("require('node:net').Socket.prototype.connect = function () { throw new Error('NETWORK_DISABLED_IN_TEST'); };", encoding='utf-8')
            args, out = work / 'args.json', work / 'result.json'
            args.write_text(json.dumps({'url': ELM.LOGIN_URL, 'session': {}, 'noNetwork': True,
                                        'umidToken': 'offline-test-umid', 'debug': False}))
            original = {p.relative_to(assets): hashlib.sha256(p.read_bytes()).digest()
                        for p in assets.rglob('*') if p.is_file()}
            result = subprocess.run([NODE, '--require', str(guard), str(assets / 'awsc_bridge.cjs'),
                                     str(args), str(out)], cwd=assets, capture_output=True,
                                    timeout=35, check=False)
            self.assertEqual(result.returncode, 0, 'Offline parameter bridge failed')
            self.assertTrue(out.is_file())
            value = json.loads(out.read_text(encoding='utf-8-sig'))
            self.assertIs(value.get('ok'), True)
            headers = value.get('headers') or {}
            self.assertTrue(all(headers.get(key) for key in ('bx-ua', 'mini-janus', 'bx-umidtoken')))
            for rel, digest in original.items():
                self.assertEqual(hashlib.sha256((assets / rel).read_bytes()).digest(), digest)


if __name__ == '__main__':
    unittest.main()
