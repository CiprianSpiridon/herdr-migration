import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('installer', Path(__file__).resolve().parents[1] / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.which = patch.object(installer.shutil, 'which', return_value='/installed/herdr-app')
        self.check = patch.object(installer.subprocess, 'run')
        self.which.start(); self.check.start()

    def tearDown(self):
        self.which.stop(); self.check.stop()
        self.temp.cleanup()

    def test_installs_only_the_mod_and_shortcuts(self):
        installer.install(self.home)
        self.assertTrue((self.home / 'mods/herdr-migration/main.js').exists())
        self.assertFalse((self.home / 'layouts').exists())
        self.assertIn('cmd-shift-m', json.loads((self.home / 'keys.json').read_text())['global'])

    def test_existing_binding_is_preserved(self):
        (self.home / 'keys.json').write_text(json.dumps({'global': {'cmd-shift-m': 'my-personal-command'}}))
        installer.install(self.home)
        self.assertEqual(json.loads((self.home / 'keys.json').read_text())['global']['cmd-shift-m'], 'my-personal-command')

    def test_identical_second_install_is_idempotent(self):
        installer.install(self.home)
        before = (self.home / 'keys.json').read_text()
        installer.install(self.home)
        self.assertEqual((self.home / 'keys.json').read_text(), before)

    def test_different_existing_mod_is_never_overwritten(self):
        target = self.home / 'mods/herdr-migration'
        target.mkdir(parents=True)
        (target / 'main.js').write_text('// my personal mod')
        with self.assertRaisesRegex(RuntimeError, 'nothing was replaced'):
            installer.install(self.home)
        self.assertEqual((target / 'main.js').read_text(), '// my personal mod')


if __name__ == '__main__':
    unittest.main()
