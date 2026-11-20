"""Package stamping must not turn build provenance into application identity."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / 'misc/bump_version.py'


class PackageVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.files = {
            'indi_allsky/version.py': '__version__ = "indi_v2026.11.01"\n__config_level__ = "20260928.0"\n',
            'package.json': '{"version":"2026.11.01"}\n',
            'indi_allsky/flask/templates/base.html': "{{ release if release else 'v2026.11.01' }}\n",
        }
        for name, text in self.files.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        (self.root / 'debian').mkdir()
        (self.root / 'misc').mkdir()
        self.script = self.root / 'misc/bump_version.py'
        shutil.copyfile(SCRIPT, self.script)
        spec = importlib.util.spec_from_file_location('isolated_version_bumper', self.script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.version = '2026.11.11+trixie.qhy.globalmap.r20261120.120000.01234567'

    def assert_source_unchanged(self):
        for name, text in self.files.items():
            self.assertEqual((self.root / name).read_bytes(), text.encode(), name)

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(self.script), *args],
                              check=True, capture_output=True, text=True)

    def test_package_only_function_preserves_source_bytes(self):
        self.module.set_package_version(self.version, suite='trixie')
        self.assert_source_unchanged()
        self.assertTrue((self.root / 'debian/changelog').read_text().startswith(
            f'indi-allsky ({self.version}) trixie; urgency=medium\n'))
        self.assertEqual(self.module.get_current_version(), '2026.11.01')

    def test_package_only_cli_preserves_identity_and_show(self):
        self.cli('--package-only', self.version, 'trixie')
        self.assert_source_unchanged()
        self.assertIn('2026.11.01', self.cli('--show').stdout)
        self.assertNotIn('2026.11.11', self.cli('--show').stdout)

    def test_same_package_version_is_idempotent(self):
        self.module.set_package_version(self.version)
        before = (self.root / 'debian/changelog').read_bytes()
        self.module.set_package_version(self.version)
        self.assertEqual((self.root / 'debian/changelog').read_bytes(), before)
        self.assert_source_unchanged()

    def test_distinct_build_versions_preserve_previous_history(self):
        self.module.set_package_version(self.version)
        before = (self.root / 'debian/changelog').read_text()
        next_build = self.version.replace('120000', '120001')
        self.module.set_package_version(next_build)
        text = (self.root / 'debian/changelog').read_text()
        self.assertTrue(text.startswith(f'indi-allsky ({next_build})'))
        self.assertTrue(text.endswith(before))
        self.assert_source_unchanged()

    def test_explicit_release_bump_keeps_previous_behaviour(self):
        self.module.set_version('indi_v2026.12.01')
        self.assertIn('__version__ = "indi_v2026.12.01"',
                      (self.root / 'indi_allsky/version.py').read_text())
        self.assertIn('__config_level__ = "20260928.0"',
                      (self.root / 'indi_allsky/version.py').read_text())
        self.assertEqual(json.loads((self.root / 'package.json').read_text())['version'],
                         '2026.12.01')
        self.assertIn('v2026.12.01',
                      (self.root / 'indi_allsky/flask/templates/base.html').read_text())
        self.assertIn('indi-allsky (2026.12.01)',
                      (self.root / 'debian/changelog').read_text())

    def test_missing_package_version_fails_without_writes(self):
        result = subprocess.run([sys.executable, '-B', str(self.script), '--package-only'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'debian/changelog').exists())
        self.assert_source_unchanged()


if __name__ == '__main__':
    unittest.main()
