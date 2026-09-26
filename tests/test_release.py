import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


BUILD = module('wsdlc_release_build', 'build_release.py')
ASSETS = module('wsdlc_release_assets', 'prepare_release_assets.py')


class ReleaseVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Release test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root / 'pyproject.toml').write_text(f'[project]\nversion = "{BUILD.VERSION}"\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.tag = 'v' + BUILD.VERSION
        self.git('tag', self.tag)
        patch = mock.patch.object(BUILD, 'ROOT', self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def test_exact_tag_returns_the_tagged_commit(self):
        self.assertEqual(BUILD.validate_release_tag(self.tag), self.git('rev-parse', 'HEAD'))

    def test_mismatched_version_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'must match'):
            BUILD.validate_release_tag('v999.0.0')
        (self.root / 'pyproject.toml').write_text('[project]\nversion = "999.0.0"\n')
        with self.assertRaisesRegex(RuntimeError, 'version mismatch'):
            BUILD.validate_release_tag(self.tag)

    def test_building_a_different_commit_under_an_old_tag_is_rejected(self):
        (self.root / 'new-file').write_text('new source')
        self.git('add', '.')
        self.git('commit', '-qm', 'new commit')
        with self.assertRaisesRegex(RuntimeError, "tag's commit"):
            BUILD.validate_release_tag(self.tag)

    def test_modified_tagged_source_is_rejected(self):
        (self.root / 'injected-module.py').write_text('unexpected source')
        with self.assertRaisesRegex(RuntimeError, 'clean tagged checkout'):
            BUILD.validate_release_tag(self.tag)

    def test_binary_cannot_be_mislabeled_as_intel(self):
        with mock.patch.object(BUILD, 'output', return_value='arm64'):
            with self.assertRaisesRegex(RuntimeError, 'architecture mismatch'):
                BUILD.verify_native_binary(Path('/tmp/test-binary'), 'x86_64')

    def test_frozen_cli_version_must_match_the_tagged_sources(self):
        with mock.patch.object(BUILD, 'output', side_effect=['arm64', 'wir-schaffen-dlc 999.0.0']):
            with self.assertRaisesRegex(RuntimeError, 'version check failed'):
                BUILD.verify_native_binary(Path('/tmp/test-binary'), 'arm64')

    def test_official_toolchain_uses_the_installed_certificate_distribution_version(self):
        (self.root / '.github').mkdir()
        python_version = (ROOT / '.github/release-python-version').read_text().strip()
        requirements = (ROOT / 'requirements-release.txt').read_text()
        (self.root / '.github/release-python-version').write_text(python_version)
        (self.root / 'requirements-release.txt').write_text(requirements)
        pyinstaller = re.search(r'^pyinstaller==([^\s]+)', requirements, re.MULTILINE)[1]
        def tool_output(command, **kwargs):
            if command[-2:] == ['PyInstaller', '--version']:
                return pyinstaller
            script = command[-1]
            if 'platform.machine' in script:
                return 'arm64'
            if 'platform.python_version' in script:
                return python_version
            # Exercise the real installed package: certifi's __version__ may
            # contain zero-padded calendar fields that its wheel normalizes.
            return subprocess.check_output([sys.executable, '-c', script], text=True).strip()
        with mock.patch.object(BUILD, 'output', side_effect=tool_output):
            BUILD.validate_toolchain(BUILD.Toolchain('arm64', Path(sys.executable)), official=True)


class ReleaseAssemblyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.version = '1.2.3'
        self.tag = 'v' + self.version
        self.commit = 'a' * 40
        for architecture, label in [('arm64', 'arm64'), ('x86_64', 'amd64')]:
            names = [f'Wir-Schaffen-DLC-{self.version}-macos-15-{label}.zip']
            artifacts = {}
            for name in names:
                (self.root / name).write_bytes(name.encode())
                artifacts[name] = ASSETS.digest(self.root / name)
            record = {
                'schema_version': 1, 'version': self.version, 'tag': self.tag,
                'source_commit': self.commit, 'architecture': architecture, 'asset_architecture': label,
                'artifacts': artifacts, 'python': '3.13.15', 'pyinstaller': '6.22.3', 'macos': '15.7',
                'build_requirements_sha256': ASSETS.digest(ROOT / 'requirements-release.txt'),
            }
            (self.root / f'build-info-{self.version}-{label}.json').write_text(json.dumps(record))

    def prepare(self):
        return ASSETS.prepare(self.root, self.tag, self.commit, 'AngelaDMerkel/Wir-Schaffen-DLC')

    def edit_metadata(self, label, **values):
        path = self.root / f'build-info-{self.version}-{label}.json'
        record = json.loads(path.read_text())
        record.update(values)
        path.write_text(json.dumps(record))

    def test_both_architectures_get_one_pinned_manifest_and_checksum_inventory(self):
        manifest = self.prepare()
        self.assertEqual(len(manifest['artifacts']), 4)
        self.assertEqual(set(manifest['public_assets']), {
            f'Wir-Schaffen-DLC-{self.version}-macos-15-arm64.zip',
            f'Wir-Schaffen-DLC-{self.version}-macos-15-amd64.zip',
        })
        self.assertEqual(manifest['source_commit'], self.commit)
        for name, record in manifest['artifacts'].items():
            self.assertEqual(record['sha256'], ASSETS.digest(self.root / name))
            if name in manifest['public_assets']:
                self.assertIn('/releases/download/v1.2.3/', record['url'])
            else:
                self.assertNotIn('url', record)
        sums = dict(line.split('  ', 1)[::-1] for line in (self.root / 'SHA256SUMS.txt').read_text().splitlines())
        self.assertEqual(set(sums), {path.name for path in self.root.iterdir()} - {'SHA256SUMS.txt'})
        for name, sha in sums.items():
            self.assertEqual(sha, ASSETS.digest(self.root / name))

    def test_missing_intel_build_prevents_publication(self):
        (self.root / f'build-info-{self.version}-amd64.json').unlink()
        with self.assertRaisesRegex(ValueError, 'missing build metadata'):
            self.prepare()
        self.assertFalse((self.root / 'release-manifest.json').exists())

    def test_tampered_download_prevents_publication(self):
        (self.root / f'Wir-Schaffen-DLC-{self.version}-macos-15-arm64.zip').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.prepare()

    def test_mixed_source_commits_prevent_publication(self):
        self.edit_metadata('amd64', source_commit='c' * 40)
        with self.assertRaisesRegex(ValueError, 'source_commit'):
            self.prepare()

    def test_mixed_toolchain_pins_prevent_publication(self):
        self.edit_metadata('amd64', build_requirements_sha256='c' * 64)
        with self.assertRaisesRegex(ValueError, 'different build dependencies'):
            self.prepare()

    def test_wrong_architecture_metadata_prevents_publication(self):
        self.edit_metadata('amd64', architecture='arm64')
        with self.assertRaisesRegex(ValueError, 'architecture'):
            self.prepare()

    def test_unpinned_python_prevents_publication(self):
        self.edit_metadata('amd64', python='3.14.6')
        with self.assertRaisesRegex(ValueError, 'python'):
            self.prepare()

    def test_unlisted_assets_are_rejected(self):
        (self.root / 'unverified.zip').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'unexpected files'):
            self.prepare()

    def test_python_distributions_are_rejected_even_when_listed_by_a_build(self):
        path = self.root / f'civ5_mod_dlc_packer-{self.version}-py3-none-any.whl'
        path.write_bytes(b'not a public native bundle')
        metadata = self.root / f'build-info-{self.version}-arm64.json'
        record = json.loads(metadata.read_text())
        record['artifacts'][path.name] = ASSETS.digest(path)
        metadata.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'unexpected artifact'):
            self.prepare()

    def test_symbolic_link_assets_are_rejected(self):
        target = self.root / f'Wir-Schaffen-DLC-{self.version}-macos-15-arm64.zip'
        contents = target.read_bytes()
        target.unlink()
        target.symlink_to(self.root / 'outside.zip')
        (self.root / 'outside.zip').write_bytes(contents)
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.prepare()


if __name__ == '__main__':
    unittest.main()
