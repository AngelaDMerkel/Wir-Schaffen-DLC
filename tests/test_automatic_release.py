import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('automatic_release', ROOT / 'scripts/automatic_release.py')
AUTO = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = AUTO
spec.loader.exec_module(AUTO)


class FakeGitHub:
    def __init__(self):
        self.release = None
        self.calls = []
        self.fail_upload_once = False
        self.corrupt_upload = False

    def find_release(self, tag):
        return copy.deepcopy(self.release)

    def create_draft(self, plan, body):
        self.calls.append('draft')
        self.release = {'id': 1, 'tag_name': plan['tag'], 'body': body, 'draft': True,
                        'immutable': False, 'assets': [], 'html_url': 'https://github.com/example/repo/releases/tag/' + plan['tag']}
        return copy.deepcopy(self.release)

    def upload(self, tag, assets):
        self.calls.append('upload')
        if not self.release['draft']:
            raise AssertionError('attempted upload to a published release')
        self.release['assets'] = [{'name': p.name, 'size': p.stat().st_size, 'state': 'uploaded',
                                   'digest': 'sha256:' + AUTO.sha256(p)} for p in assets]
        if self.fail_upload_once:
            self.fail_upload_once = False
            self.release['assets'] = self.release['assets'][:1]
            raise OSError('simulated interrupted upload')
        if self.corrupt_upload:
            self.release['assets'][0]['digest'] = 'sha256:' + '0' * 64

    def get_release(self, release_id):
        return copy.deepcopy(self.release)

    def publish(self, release_id, latest):
        self.calls.append(('publish', latest))
        self.release['draft'] = False
        self.release['immutable'] = True
        return copy.deepcopy(self.release)


class AutomaticReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / 'source'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'pyproject.toml').write_text('[project]\nversion = "0.5.0"\n')
        (self.repo / 'civ5_dlc_packer.py').write_text('VERSION = "0.5.0"\n')
        (self.repo / 'application.py').write_text('print("unchanged application")\n')
        (self.repo / 'scripts').mkdir()
        (self.repo / 'assets').mkdir()
        (self.repo / 'scripts/render_main_menu.py').write_text('''import argparse
from pathlib import Path
import re
root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
version = re.search(r'VERSION = "([^"]+)"', (root / 'civ5_dlc_packer.py').read_text())[1]
args.output.write_text('<svg>v' + version + ':' + (root / 'application.py').read_text() + '</svg>')
''')
        (self.repo / AUTO.MENU_IMAGE).write_bytes(AUTO.menu_image(self.repo))
        self.git('add', '.')
        self.git('commit', '-qm', 'pushed source')
        self.source = self.git('rev-parse', 'HEAD')
        self.git('tag', 'v0.1.0')
        self.git('tag', 'v1.0.0')
        self.remote = self.base / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-q', 'origin', 'main')
        self.directory = self.base / 'plan'

    def git(self, *args):
        return AUTO.git(self.repo, *args)

    def plan(self, run_id='100', run_number=7):
        return AUTO.make_plan(self.repo, self.source, run_id, run_number, self.directory)

    def assets(self, plan):
        directory = self.base / 'assets'
        directory.mkdir(exist_ok=True)
        artifacts = {}
        public_assets = []
        for arch in ('arm64', 'amd64'):
            path = directory / f'Wir-Schaffen-DLC-{plan["version"]}-macos-15-{arch}.zip'
            path.write_bytes((arch + ' synthetic fixture').encode())
            artifacts[path.name] = {'sha256': AUTO.sha256(path)}
            public_assets.append(path.name)
            info = directory / f'build-info-{plan["version"]}-{arch}.json'
            info.write_text(json.dumps({'architecture': arch, 'source_commit': plan['release_commit']}))
            artifacts[info.name] = {'sha256': AUTO.sha256(info)}
        manifest = {'tag': plan['tag'], 'version': plan['version'], 'source_commit': plan['release_commit'],
                    'artifacts': artifacts, 'public_assets': public_assets}
        (directory / 'release-manifest.json').write_text(json.dumps(manifest))
        (directory / 'SHA256SUMS.txt').write_text(''.join(f'{AUTO.sha256(p)}  {p.name}\n' for p in sorted(directory.iterdir()) if p.name != 'SHA256SUMS.txt'))
        return directory

    def test_next_version_handles_existing_tags_and_optional_major_minor_bump(self):
        self.assertEqual(AUTO.next_version('0.5.0', []), '0.5.0')
        self.assertEqual(AUTO.next_version('0.5.0', ['v0.1.0', 'v1.0.0']), '1.0.1')
        self.assertEqual(AUTO.next_version('0.5.0', ['v1.0.9', 'v1.0.10', 'unrelated']), '1.0.11')
        self.assertEqual(AUTO.next_version('2.0.0', ['v1.0.9']), '2.0.0')

    def test_versions_and_tag_are_generated_without_modifying_source_branch(self):
        plan = self.plan()
        self.assertEqual(plan['tag'], 'v1.0.1')
        self.assertEqual(AUTO.versions(self.repo), ['1.0.1', '1.0.1'])
        self.assertEqual(self.git('rev-parse', 'main'), self.source)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), self.source)
        self.assertEqual(set(self.git('diff', '--name-only', self.source, 'HEAD').splitlines()), set(AUTO.RELEASE_FILES))
        self.assertIn('v1.0.1', (self.repo / AUTO.MENU_IMAGE).read_text())
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/tags/v1.0.1'), '')
        self.assertEqual(AUTO.tag_plan(self.repo, 'v1.0.1')['run_id'], '100')

    def test_both_jobs_restore_the_identical_release_commit(self):
        plan = self.plan()
        for label in ['arm64', 'amd64']:
            clone = self.base / label
            subprocess.run(['git', 'clone', '-q', str(self.remote), str(clone)], check=True)
            # The bare fixture's default branch may follow the machine default.
            AUTO.git(clone, 'checkout', '--quiet', '--detach', self.source)
            restored = AUTO.restore_plan(clone, self.directory)
            self.assertEqual(restored, plan)
            self.assertEqual(AUTO.git(clone, 'rev-parse', 'HEAD'), plan['release_commit'])
            self.assertEqual(AUTO.versions(clone), ['1.0.1', '1.0.1'])

    def test_rerun_reuses_its_tag_after_later_versions_exist(self):
        first = self.plan()
        self.git('checkout', '--quiet', '--detach', self.source)
        self.git('tag', 'v5.0.0')
        retry = self.plan()
        self.assertEqual(retry['tag'], first['tag'])
        self.assertEqual(retry['release_commit'], first['release_commit'])

    def test_next_push_automatically_gets_the_next_patch(self):
        self.plan()
        self.git('checkout', '--quiet', '--detach', self.source)
        next_plan = self.plan('101', 8)
        self.assertEqual(next_plan['tag'], 'v1.0.2')

    def test_uncommitted_source_is_rejected(self):
        (self.repo / 'application.py').write_text('uncommitted change')
        with self.assertRaisesRegex(ValueError, 'clean source'):
            self.plan()

    def test_corrupted_bundle_cannot_be_built(self):
        self.plan()
        self.git('checkout', '--quiet', '--detach', self.source)
        (self.directory / 'source.bundle').write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            AUTO.restore_plan(self.repo, self.directory)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.source)

    def test_complete_draft_is_verified_before_immutable_publication(self):
        plan = self.plan()
        github = FakeGitHub()
        result = AUTO.publish_release(self.repo, plan, self.assets(plan), github)
        self.assertEqual(github.calls, ['draft', 'upload', ('publish', True)])
        self.assertFalse(result['draft'])
        self.assertTrue(result['immutable'])
        self.assertEqual({asset['name'] for asset in result['assets']}, {
            f'Wir-Schaffen-DLC-{plan["version"]}-macos-15-arm64.zip',
            f'Wir-Schaffen-DLC-{plan["version"]}-macos-15-amd64.zip',
        })
        self.assertNotIn('SHA256SUMS.txt', result['body'])
        self.assertNotIn('release-manifest.json', result['body'])
        self.assertIn('refs/tags/v1.0.1', self.git('ls-remote', 'origin', 'refs/tags/v1.0.1'))
        self.assertIn(self.source, self.git('ls-remote', 'origin', 'refs/heads/main'))

    def test_published_release_is_not_uploaded_again(self):
        plan = self.plan()
        assets, github = self.assets(plan), FakeGitHub()
        AUTO.publish_release(self.repo, plan, assets, github)
        github.calls.clear()
        AUTO.publish_release(self.repo, plan, assets, github)
        self.assertEqual(github.calls, [])

    def test_interrupted_draft_upload_recovers_without_manual_asset_removal(self):
        plan = self.plan()
        assets, github = self.assets(plan), FakeGitHub()
        github.fail_upload_once = True
        with self.assertRaises(OSError):
            AUTO.publish_release(self.repo, plan, assets, github)
        self.assertTrue(github.release['draft'])
        result = AUTO.publish_release(self.repo, plan, assets, github)
        self.assertFalse(result['draft'])
        self.assertEqual(github.calls.count('draft'), 1)

    def test_remote_hash_mismatch_leaves_release_unpublished(self):
        plan = self.plan()
        github = FakeGitHub()
        github.corrupt_upload = True
        with self.assertRaisesRegex(ValueError, 'upload verification'):
            AUTO.publish_release(self.repo, plan, self.assets(plan), github)
        self.assertTrue(github.release['draft'])
        self.assertNotIn(('publish', True), github.calls)

    def test_unrelated_release_is_preserved(self):
        plan = self.plan()
        github = FakeGitHub()
        github.release = {'body': 'User-authored release notes', 'draft': True}
        with self.assertRaisesRegex(ValueError, 'another run or user'):
            AUTO.publish_release(self.repo, plan, self.assets(plan), github)
        self.assertEqual(github.calls, [])

    def test_local_tampering_is_rejected_before_tag_push(self):
        plan = self.plan()
        assets, github = self.assets(plan), FakeGitHub()
        next(assets.glob('*.zip')).write_bytes(b'wrong bytes')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            AUTO.publish_release(self.repo, plan, assets, github)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/tags/v1.0.1'), '')

    def test_internal_build_records_are_still_verified_before_publication(self):
        plan = self.plan()
        assets, github = self.assets(plan), FakeGitHub()
        next(assets.glob('build-info-*.json')).write_bytes(b'corrupt build metadata')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            AUTO.publish_release(self.repo, plan, assets, github)
        self.assertEqual(github.calls, [])

    def test_metadata_cannot_be_added_to_the_public_download_list(self):
        plan = self.plan()
        assets, github = self.assets(plan), FakeGitHub()
        path = assets / 'release-manifest.json'
        manifest = json.loads(path.read_text())
        manifest['public_assets'].append(next(assets.glob('build-info-*.json')).name)
        path.write_text(json.dumps(manifest))
        (assets / 'SHA256SUMS.txt').write_text(''.join(f'{AUTO.sha256(p)}  {p.name}\n' for p in sorted(assets.iterdir()) if p.name != 'SHA256SUMS.txt'))
        with self.assertRaisesRegex(ValueError, 'exactly two executable bundles'):
            AUTO.publish_release(self.repo, plan, assets, github)
        self.assertEqual(github.calls, [])

    def test_duplicate_architecture_cannot_replace_the_other_bundle(self):
        plan = self.plan()
        assets, github = self.assets(plan), FakeGitHub()
        path = assets / 'release-manifest.json'
        manifest = json.loads(path.read_text())
        manifest['public_assets'] = [manifest['public_assets'][0]] * 2
        path.write_text(json.dumps(manifest))
        (assets / 'SHA256SUMS.txt').write_text(''.join(f'{AUTO.sha256(p)}  {p.name}\n' for p in sorted(assets.iterdir()) if p.name != 'SHA256SUMS.txt'))
        with self.assertRaisesRegex(ValueError, 'exactly two executable bundles'):
            AUTO.publish_release(self.repo, plan, assets, github)
        self.assertEqual(github.calls, [])

    def test_retry_of_older_run_does_not_displace_newer_latest_release(self):
        older = self.plan()
        self.git('checkout', '--quiet', '--detach', self.source)
        self.plan('101', 8)
        self.git('checkout', '--quiet', '--detach', older['release_commit'])
        github = FakeGitHub()
        AUTO.publish_release(self.repo, older, self.assets(older), github)
        self.assertEqual(github.calls[-1], ('publish', False))

    def published(self):
        plan, github = self.plan(), FakeGitHub()
        AUTO.publish_release(self.repo, plan, self.assets(plan), github)
        return plan, github

    def test_published_version_and_image_are_synced_without_changing_application_code(self):
        plan, github = self.published()
        AUTO.sync_source_branch(self.repo, plan, 'main', github)
        head = self.git('rev-parse', 'HEAD')
        self.assertIn(head, self.git('ls-remote', 'origin', 'refs/heads/main'))
        self.assertEqual(AUTO.versions(self.repo), ['1.0.1', '1.0.1'])
        self.assertEqual((self.repo / AUTO.MENU_IMAGE).read_bytes(), AUTO.menu_image(self.repo))
        self.assertEqual(set(self.git('diff', '--name-only', self.source, head).splitlines()), set(AUTO.RELEASE_FILES))
        self.assertIn('already synchronized', AUTO.sync_source_branch(self.repo, plan, 'main', github))
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)

    def test_unpublished_release_cannot_update_source_versions(self):
        plan, github = self.plan(), FakeGitHub()
        github.create_draft(plan, AUTO.release_marker(plan))
        with self.assertRaisesRegex(ValueError, 'published release'):
            AUTO.sync_source_branch(self.repo, plan, 'main', github)
        self.assertIn(self.source, self.git('ls-remote', 'origin', 'refs/heads/main'))

    def test_new_source_changes_are_preserved_and_used_to_render_the_synced_image(self):
        plan, github = self.published()
        self.git('checkout', '--quiet', 'main')
        (self.repo / 'application.py').write_text('newer menu layout\n')
        self.git('add', 'application.py')
        self.git('commit', '-qm', 'concurrent user change')
        newer = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'origin', 'main')
        self.git('checkout', '--quiet', '--detach', plan['release_commit'])
        AUTO.sync_source_branch(self.repo, plan, 'main', github)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), newer)
        self.assertEqual((self.repo / 'application.py').read_text(), 'newer menu layout\n')
        self.assertIn('newer menu layout', (self.repo / AUTO.MENU_IMAGE).read_text())

    def test_push_race_refetches_and_preserves_the_new_user_commit(self):
        plan, github = self.published()
        competitor = self.base / 'competitor'
        subprocess.run(['git', 'clone', '-q', '-b', 'main', str(self.remote), str(competitor)], check=True)
        real_git = AUTO.git
        raced = []
        def race(root, *args, **kwargs):
            if root == self.repo and args == ('push', 'origin', 'HEAD:refs/heads/main') and not raced:
                (competitor / 'application.py').write_text('concurrent change during push\n')
                real_git(competitor, 'add', 'application.py')
                real_git(competitor, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                         'commit', '-qm', 'racing user commit')
                raced.append(real_git(competitor, 'rev-parse', 'HEAD'))
                real_git(competitor, 'push', '-q', 'origin', 'main')
            return real_git(root, *args, **kwargs)
        with mock.patch.object(AUTO, 'git', side_effect=race):
            AUTO.sync_source_branch(self.repo, plan, 'main', github)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), raced[0])
        self.assertEqual((self.repo / 'application.py').read_text(), 'concurrent change during push\n')
        self.assertIn('concurrent change during push', (self.repo / AUTO.MENU_IMAGE).read_text())

    def test_older_release_retry_cannot_downgrade_a_newer_source_version(self):
        plan, github = self.published()
        self.git('checkout', '--quiet', 'main')
        AUTO.stamp_release_files(self.repo, '2.0.0')
        self.git('add', '.')
        self.git('commit', '-qm', 'newer release version')
        newer = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'origin', 'main')
        self.git('checkout', '--quiet', '--detach', plan['release_commit'])
        self.assertIn('retained newer', AUTO.sync_source_branch(self.repo, plan, 'main', github))
        self.assertEqual(self.git('rev-parse', 'HEAD'), newer)
        self.assertEqual(AUTO.versions(self.repo), ['2.0.0', '2.0.0'])

    def test_both_publishing_branches_receive_the_published_version(self):
        self.git('push', '-q', 'origin', self.source + ':refs/heads/codex/shared-macos-gamecore')
        plan, github = self.published()
        heads = []
        for branch in AUTO.PUBLISH_BRANCHES:
            AUTO.sync_source_branch(self.repo, plan, branch, github)
            heads.append(self.git('rev-parse', 'HEAD'))
            self.assertIn(heads[-1], self.git('ls-remote', 'origin', 'refs/heads/' + branch))
            self.assertEqual(AUTO.versions(self.repo), ['1.0.1', '1.0.1'])
        self.assertEqual(heads[0], heads[1])

    def test_dirty_source_is_rejected_before_version_synchronization(self):
        plan, github = self.published()
        (self.repo / 'application.py').write_text('unfinished work')
        with self.assertRaisesRegex(ValueError, 'clean checkout'):
            AUTO.sync_source_branch(self.repo, plan, 'main', github)
        self.assertEqual((self.repo / 'application.py').read_text(), 'unfinished work')

    def test_stale_release_image_prevents_publication(self):
        plan = self.plan()
        (self.repo / AUTO.MENU_IMAGE).write_text('<svg>v0.5.0</svg>')
        with self.assertRaisesRegex(ValueError, 'README image'):
            AUTO.publish_release(self.repo, plan, self.assets(plan), FakeGitHub())


if __name__ == '__main__':
    unittest.main()
