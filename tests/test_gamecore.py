import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import civ5_gamecore as core


class GameCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / 'Civilization V.app'
        self.user = self.root / 'user'
        self.binary = self.app / core.BINARY_RELATIVE
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b'known stock GameCore')
        self.host = self.binary.parent / 'Civilization V'
        self.host.write_bytes(b'known host executable')
        (self.app / core.DLC_RELATIVE).mkdir(parents=True)
        self.user.mkdir()
        self.catalog = copy.deepcopy(core.CATALOG)
        self.catalog['stock_hashes'] = [core.sha256(self.binary)]
        self.catalog['host_hashes'] = [core.sha256(self.host)]
        self.catalog.pop('legacy_lekmod')
        self.catalog['verify_codesign'] = False  # fixtures contain synthetic binary bytes
        self.manager = core.ProductManager(self.app, self.user, self.catalog)

    def package(self, product='lekmod', version='1'):
        root = self.root / (product + '-' + version)
        root.mkdir()
        (root / core.BINARY).write_bytes((product + '-binary-' + version).encode())
        payload_name = self.catalog['products'][product]['payload']
        payload = root / 'payload' / payload_name
        payload.mkdir(parents=True)
        (payload / 'Product.Civ5Pkg').write_text('<Civ5Package/>')
        (payload / 'Content.xml').write_text('<GameData>' + version + '</GameData>')
        (root / 'licenses').mkdir()
        (root / 'licenses/source.txt').write_text('fixture license')
        files = core.tree_files(root)
        manifest = {
            'schema_version': 1, 'product': product, 'version': version, 'abi_id': core.ABI,
            'source': {'repository': self.catalog['products'][product]['repository'], 'commit': 'a' * 40},
            'compat': {'repository': 'https://github.com/AngelaDMerkel/civ5-macos-gamecore-compat', 'commit': 'b' * 40},
            'supported_stock_game_hashes': {'gamecore': self.catalog['stock_hashes'], 'executable': self.catalog['host_hashes']},
            'gamecore_sha256': files[core.BINARY], 'payload_sha256': core.tree_hash({k: v for k, v in files.items() if k.startswith('payload/')}),
            'files': files, 'conflicts': list(set(self.catalog['products']) - {product}),
            'destinations': {'gamecore': core.BINARY_RELATIVE, 'payload': {payload_name: core.DLC_RELATIVE + '/' + payload_name}},
            'licenses': ['licenses/source.txt'],
        }
        (root / 'manifest.json').write_text(json.dumps(manifest))
        sums = core.tree_files(root)
        (root / 'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in sorted(sums.items())))
        return root

    def archive(self, package):
        archive = package.with_suffix('.zip')
        with zipfile.ZipFile(archive, 'w') as writer:
            for path in package.rglob('*'):
                if path.is_file():
                    writer.write(path, path.relative_to(package))
        return archive

    def test_install_update_switch_restore_and_single_stock_backup(self):
        original = self.binary.read_bytes()
        lek = self.package()
        vp = self.package('vox-populi')
        updated = self.package(version='2')
        cache = self.user / 'cache/Civ5CoreDatabase.db'
        cache.parent.mkdir()
        cache.write_bytes(b'cached old database')
        self.manager.switch('lekmod', lek)
        self.assertEqual(self.manager.status()['product'], 'lekmod')
        self.assertFalse(cache.exists())
        self.manager.switch('lekmod', updated)
        self.assertEqual(self.manager.status()['version'], '2')
        self.manager.switch('vox-populi', vp)
        self.assertFalse(self.manager._payload_path('lekmod').exists())
        self.assertTrue(self.manager._payload_path('vox-populi').is_dir())
        self.assertEqual(self.manager.backup.read_bytes(), original)
        self.manager.switch('stock')
        self.assertEqual(self.binary.read_bytes(), original)
        self.assertEqual(self.manager.status()['product'], 'stock')
        self.assertFalse(self.manager._payload_path('vox-populi').exists())
        self.assertFalse(list(self.binary.parent.glob('*.lekmod-original')))
        self.assertFalse(list(self.binary.parent.glob('*.vp-original')))

    def test_dry_run_and_status_do_not_create_state(self):
        self.assertEqual(self.manager.status()['status'], 'stock')
        plan = self.manager.switch('lekmod', self.package(), dry_run=True)
        self.assertEqual(plan['to'], 'lekmod')
        self.assertFalse(self.manager.root.exists())
        self.assertEqual(self.binary.read_bytes(), b'known stock GameCore')

    def test_rollback_after_binary_write_failure(self):
        self.manager.switch('lekmod', self.package())
        before = self.binary.read_bytes()
        state_before = self.manager.state_path.read_bytes()
        real_replace = self.manager._replace
        failures = []
        def fail_once(destination, source):
            real_replace(destination, source)
            if destination == self.binary and not failures:
                failures.append(True)
                raise OSError('injected failure after replacing binary')
        with patch.object(self.manager, '_replace', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.manager.switch('vox-populi', self.package('vox-populi'))
        self.assertEqual(self.binary.read_bytes(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), state_before)
        self.assertTrue(self.manager._payload_path('lekmod').exists())
        self.assertFalse(self.manager._payload_path('vox-populi').exists())

    def test_interrupted_transaction_recovery(self):
        # Simulate abrupt process death: rollback itself is not reached.
        real_replace = self.manager._replace
        def fail(destination, source):
            real_replace(destination, source)
            if destination == self.binary:
                raise OSError('simulated interruption')
        with patch.object(self.manager, '_replace', side_effect=fail), patch.object(self.manager, '_recover_locked'):
            with self.assertRaises(OSError):
                self.manager.switch('lekmod', self.package())
        self.assertEqual(self.manager.status()['status'], 'interrupted')
        core.ProductManager(self.app, self.user, self.catalog).recover()
        self.assertEqual(self.manager.status()['status'], 'stock')
        self.assertFalse(self.manager._payload_path('lekmod').exists())

    def test_steam_integrity_restoration_is_detected(self):
        self.manager.switch('lekmod', self.package())
        self.binary.write_bytes(self.manager.backup.read_bytes())
        self.assertEqual(self.manager.status()['status'], 'steam-restored')
        self.manager.switch('stock')
        self.assertFalse(self.manager._payload_path('lekmod').exists())

    def test_unknown_binary_or_host_is_never_backed_up(self):
        self.binary.write_bytes(b'another mod')
        with self.assertRaisesRegex(core.GameCoreError, 'unknown-binary'):
            self.manager.switch('lekmod', self.package())
        self.assertFalse(self.manager.backup.exists())
        self.host.write_bytes(b'Steam update')
        self.assertEqual(self.manager.status()['status'], 'unsupported-host')

    def test_modified_payload_and_stock_backup_are_preserved(self):
        self.manager.switch('lekmod', self.package())
        payload = self.manager._payload_path('lekmod') / 'user-file.txt'
        payload.write_text('preserve me')
        with self.assertRaisesRegex(core.GameCoreError, 'modified-payload'):
            self.manager.switch('stock')
        self.assertEqual(payload.read_text(), 'preserve me')
        self.manager.backup.write_bytes(b'corrupt')
        with self.assertRaisesRegex(core.GameCoreError, 'backup'):
            self.manager.status()

    def test_unowned_payload_is_never_removed(self):
        path = self.manager._payload_path('vox-populi')
        path.mkdir()
        (path / 'user.txt').write_text('mine')
        with self.assertRaisesRegex(core.GameCoreError, 'unowned'):
            self.manager.switch('lekmod', self.package())
        self.assertTrue((path / 'user.txt').is_file())

    def test_legacy_migration_verifies_both_binary_and_payload(self):
        legacy = self.package()
        self.binary.with_name(core.BINARY + '.lekmod-original').write_bytes(self.binary.read_bytes())
        self.binary.write_bytes((legacy / core.BINARY).read_bytes())
        import shutil
        shutil.copytree(legacy / 'payload/LEKMOD', self.manager._payload_path('lekmod'))
        self.catalog['legacy_lekmod'] = {'gamecore_sha256': core.sha256(self.binary), 'payload_sha256': self.manager._payload_digest('lekmod')}
        self.assertEqual(self.manager.status()['status'], 'legacy-lekmod')
        self.manager.switch('stock')
        self.assertEqual(core.sha256(self.binary), self.catalog['stock_hashes'][0])
        self.assertEqual(core.sha256(self.manager.backup), self.catalog['stock_hashes'][0])

    def test_verified_archive_and_independent_digest(self):
        archive = self.archive(self.package())
        with core.open_artifact(archive, core.sha256(archive), self.catalog) as (directory, manifest):
            self.assertEqual(manifest['product'], 'lekmod')
            self.assertTrue((directory / core.BINARY).is_file())
        with self.assertRaises(core.GameCoreError):
            with core.open_artifact(archive, '0' * 64, self.catalog):
                self.fail('wrong digest accepted')

    def test_tampering_and_unlisted_files_fail(self):
        package = self.package()
        (package / core.BINARY).write_bytes(b'tampered')
        with self.assertRaisesRegex(core.GameCoreError, 'hash mismatch'):
            core.verify_package(package, self.catalog)
        (package / 'surprise').write_text('extra file')
        with self.assertRaisesRegex(core.GameCoreError, 'inventory'):
            core.verify_package(package, self.catalog)

    def test_path_traversal_case_collisions_and_symlinks_fail(self):
        for index, entries in enumerate([['../escape'], ['/tmp/escape'], ['a', 'A'], ['a/../escape'], ['a\\escape']]):
            archive = self.root / f'bad-{index}.zip'
            with zipfile.ZipFile(archive, 'w') as writer:
                for entry in entries:
                    writer.writestr(entry, 'bad')
            with self.assertRaises(core.GameCoreError):
                with core.open_artifact(archive, core.sha256(archive), self.catalog):
                    self.fail('unsafe archive accepted')
        outside = self.root / 'outside'
        outside.mkdir()
        self.manager._payload_path('lekmod').symlink_to(outside)
        with self.assertRaisesRegex(core.GameCoreError, 'symlink'):
            self.manager.switch('lekmod', self.package())

    def test_wrong_repository_destination_or_stock_product_artifact_fails(self):
        package = self.package()
        path = package / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['source']['repository'] = 'https://example.com/evil'
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(core.GameCoreError, 'not trusted'):
            core.verify_package(package, self.catalog)
        good = self.package(version='2')
        with self.assertRaisesRegex(core.GameCoreError, 'stock restoration'):
            self.manager.switch('stock', good)

    def test_cli_dry_run_dispatch_does_not_write_application(self):
        import civ5_dlc_installer as installer
        from types import SimpleNamespace
        package = self.package()
        archive = self.archive(package)
        args = installer.make_parser().parse_args([
            '--gamecore', 'lekmod', '--gamecore-package', str(archive),
            '--gamecore-sha256', core.sha256(archive), '--dry-run'])
        install = SimpleNamespace(game_app=self.app, user_data=self.user)
        messages = []
        original_open = core.open_artifact
        with patch.object(core, 'ProductManager', return_value=self.manager), patch.object(
            core, 'open_artifact', side_effect=lambda path, digest: original_open(path, digest, self.catalog)
        ):
            result = installer.run_gamecore(args, install, lambda question: self.fail(question), messages.append)
        self.assertEqual(result, 0)
        self.assertFalse(self.manager.root.exists())
        self.assertIn('Dry run complete', messages[-1])

    def test_concurrent_switch_is_rejected(self):
        import fcntl
        self.manager.root.mkdir(parents=True)
        with (self.manager.root / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(core.GameCoreError, 'another GameCore'):
                self.manager.switch('lekmod', self.package())

    def test_corrupt_recovery_snapshot_does_not_overwrite_live_data(self):
        real_replace = self.manager._replace
        def fail(destination, source):
            real_replace(destination, source)
            if destination == self.binary:
                raise OSError('interruption')
        with patch.object(self.manager, '_replace', side_effect=fail), patch.object(self.manager, '_recover_locked'):
            with self.assertRaises(OSError):
                self.manager.switch('lekmod', self.package())
        live = self.binary.read_bytes()
        (self.manager.transaction / 'before/binary').write_bytes(b'corrupted snapshot')
        with self.assertRaisesRegex(core.GameCoreError, 'recovery backup'):
            self.manager.recover()
        self.assertEqual(self.binary.read_bytes(), live)

    def test_steam_host_update_blocks_stale_transaction_recovery(self):
        real_replace = self.manager._replace
        def fail(destination, source):
            real_replace(destination, source)
            if destination == self.binary:
                raise OSError('interruption')
        with patch.object(self.manager, '_replace', side_effect=fail), patch.object(self.manager, '_recover_locked'):
            with self.assertRaises(OSError):
                self.manager.switch('lekmod', self.package())
        live = self.binary.read_bytes()
        self.host.write_bytes(b'new Steam executable')
        with self.assertRaisesRegex(core.GameCoreError, 'host changed'):
            self.manager.recover()
        self.assertEqual(self.binary.read_bytes(), live)

    def test_signature_failure_blocks_an_otherwise_hashed_artifact(self):
        from types import SimpleNamespace
        package = self.package()
        self.catalog['verify_codesign'] = True
        with patch.object(core.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='invalid signature')):
            with self.assertRaisesRegex(core.GameCoreError, 'signature'):
                core.verify_package(package, self.catalog)

    def test_verified_executable_restoration_can_precede_native_maintenance(self):
        import civ5_dlc_installer as installer
        from types import SimpleNamespace
        args = installer.make_parser().parse_args([
            '--game-app', str(self.app), '--user-data', str(self.user), '--restore-engine-patch'])
        install = SimpleNamespace(game_app=self.app, user_data=self.user)
        self.binary.write_bytes(b'legacy custom GameCore')
        with patch.object(installer, 'validate_install', return_value=install), patch.object(
            installer, 'restore_colossal_engine_patch', return_value=True
        ) as restore:
            result = installer.run(args, input_fn=lambda question: self.fail(question), output_fn=lambda message: None)
        self.assertEqual(result, 0)
        restore.assert_called_once_with(self.app, self.user)


if __name__ == '__main__':
    unittest.main()
