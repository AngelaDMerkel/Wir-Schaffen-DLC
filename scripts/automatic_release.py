#!/usr/bin/env python3
"""Create exact versioned CI checkouts and publish complete automatic releases.

The workflow plans in a disposable checkout. After publication it synchronizes
the source versions and README image without changing other branch content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = 'wsdlc-automatic-release-v1'
VERSION_FILES = ('pyproject.toml', 'civ5_dlc_packer.py')
MENU_IMAGE = 'assets/wir-schaffen-dlc-main.svg'
RELEASE_FILES = (*VERSION_FILES, MENU_IMAGE)
PUBLISH_BRANCHES = ('main', 'codex/shared-macos-gamecore')


def git(root: Path, *args: str, env=None) -> str:
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, env=env).strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_tuple(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError(f'expected major.minor.patch: {value!r}')
    return tuple(map(int, value.split('.')))


def next_version(base: str, tags: list[str]) -> str:
    base_tuple = version_tuple(base)
    existing = []
    for tag in tags:
        if tag.startswith('v'):
            try:
                existing.append(version_tuple(tag[1:]))
            except ValueError:
                pass
    if not existing or base_tuple > max(existing):
        return base
    major, minor, patch = max(existing)
    return f'{major}.{minor}.{patch + 1}'


def version_pattern(filename: str) -> str:
    name = 'version' if filename == 'pyproject.toml' else 'VERSION'
    return rf'(?m)^({name}\s*=\s*")([^"\n]+)(")'


def versions(root: Path) -> list[str]:
    result = []
    for name in VERSION_FILES:
        matches = list(re.finditer(version_pattern(name), (root / name).read_text()))
        if len(matches) != 1:
            raise ValueError(f'expected one version declaration in {name}')
        result.append(matches[0][2])
    if result[0] != result[1]:
        raise ValueError('source version declarations disagree')
    version_tuple(result[0])
    return result


def menu_image(root: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix='wsdlc-menu-') as temporary:
        output = Path(temporary) / 'main.svg'
        # A fresh cache location also avoids reading stale same-size .pyc files
        # immediately after a version stamp. No cache is written into the repo.
        env = os.environ | {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPYCACHEPREFIX': str(Path(temporary) / 'cache')}
        subprocess.run([sys.executable, str(root / 'scripts/render_main_menu.py'), '--output', str(output)],
                       cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
        return output.read_bytes()


def stamp_release_files(root: Path, version: str) -> None:
    version_tuple(version)
    versions(root)
    for name in VERSION_FILES:
        path = root / name
        path.write_text(re.sub(version_pattern(name), lambda m: m[1] + version + m[3], path.read_text()))
    (root / MENU_IMAGE).write_bytes(menu_image(root))


def bot_environment(root: Path, source: str) -> dict[str, str]:
    epoch = git(root, 'show', '-s', '--format=%ct', source)
    return os.environ | {
        'GIT_AUTHOR_NAME': 'github-actions[bot]', 'GIT_COMMITTER_NAME': 'github-actions[bot]',
        'GIT_AUTHOR_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com',
        'GIT_COMMITTER_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com',
        'GIT_AUTHOR_DATE': f'{epoch} +0000', 'GIT_COMMITTER_DATE': f'{epoch} +0000',
    }


def validate_plan(plan: dict) -> None:
    if plan.get('automation') != AUTOMATION or plan.get('schema_version') != 1:
        raise ValueError('unrecognized release plan')
    version_tuple(plan['version'])
    if plan['tag'] != 'v' + plan['version']:
        raise ValueError('tag/version mismatch')
    for name in ('source_commit', 'release_commit'):
        if not re.fullmatch(r'[0-9a-f]{40}', plan[name]):
            raise ValueError('expected full source/release commit identifiers')
    if not re.fullmatch(r'[1-9][0-9]*', str(plan['run_id'])) or not isinstance(plan['run_number'], int) or plan['run_number'] < 1:
        raise ValueError('invalid workflow run identity')


def tag_plan(root: Path, tag: str) -> dict | None:
    if git(root, 'cat-file', '-t', f'refs/tags/{tag}') != 'tag':
        return None
    try:
        record = json.loads(git(root, 'for-each-ref', '--format=%(contents)', f'refs/tags/{tag}'))
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get('automation') != AUTOMATION:
        return None
    validate_plan(record)
    if record['tag'] != tag or git(root, 'rev-parse', f'refs/tags/{tag}^{{commit}}') != record['release_commit']:
        raise ValueError('automatic release tag identity was modified')
    return record


def check_release_checkout(root: Path, plan: dict) -> None:
    validate_plan(plan)
    if git(root, 'rev-parse', 'HEAD') != plan['release_commit']:
        raise ValueError('checkout is not the planned release commit')
    if git(root, 'rev-list', '--parents', '-n', '1', 'HEAD').split() != [plan['release_commit'], plan['source_commit']]:
        raise ValueError('release commit must have the pushed source as its sole parent')
    changed = set(git(root, 'diff', '--name-only', plan['source_commit'], plan['release_commit']).splitlines())
    if not changed <= set(RELEASE_FILES):
        raise ValueError('release commit changed files other than versions and the README image')
    if versions(root) != [plan['version'], plan['version']]:
        raise ValueError('inconsistent stamped versions')
    if (root / MENU_IMAGE).read_bytes() != menu_image(root):
        raise ValueError('README image does not match the stamped release')
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('release checkout must be clean')
    if tag_plan(root, plan['tag']) != {k: v for k, v in plan.items() if k != 'bundle_sha256'}:
        raise ValueError('release plan differs from its annotated tag')


def make_plan(root: Path, source: str, run_id: str, run_number: int, directory: Path) -> dict:
    root, directory = root.resolve(), directory.resolve()
    if not re.fullmatch(r'[0-9a-f]{40}', source) or git(root, 'rev-parse', 'HEAD') != source:
        raise ValueError('planner must start at the exact pushed commit')
    if not re.fullmatch(r'[1-9][0-9]*', run_id) or run_number < 1:
        raise ValueError('invalid workflow run identity')
    if root == directory or root in directory.parents:
        raise ValueError('store the plan outside the source checkout')
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('planner requires a clean source checkout')
    git(root, 'checkout', '--quiet', '--detach', source)
    tags = git(root, 'tag', '--list').splitlines()
    plan = None
    for tag in tags:
        saved = tag_plan(root, tag)
        if saved and saved['run_id'] == run_id:
            if saved['source_commit'] != source or saved['run_number'] != run_number or plan is not None:
                raise ValueError('workflow run has a conflicting release identity')
            plan = saved
    if plan is None:
        version = next_version(versions(root)[0], tags)
        tag = 'v' + version
        stamp_release_files(root, version)
        env = bot_environment(root, source)
        git(root, 'add', '--', *RELEASE_FILES)
        git(root, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m',
            f'Release {tag}\n\nSource-Commit: {source}\nWorkflow-Run: {run_id}', env=env)
        plan = {'schema_version': 1, 'automation': AUTOMATION, 'version': version, 'tag': tag,
                'source_commit': source, 'release_commit': git(root, 'rev-parse', 'HEAD'),
                'run_id': run_id, 'run_number': run_number}
        git(root, '-c', 'tag.gpgSign=false', 'tag', '-a', tag, '-m', json.dumps(plan, sort_keys=True), env=env)
    else:
        git(root, 'checkout', '--quiet', '--detach', plan['release_commit'])
    check_release_checkout(root, plan)
    directory.mkdir(parents=True, exist_ok=True)
    bundle = directory / 'source.bundle'
    git(root, 'bundle', 'create', str(bundle), f'refs/tags/{plan["tag"]}', '^' + source)
    plan = dict(plan, bundle_sha256=sha256(bundle))
    (directory / 'plan.json').write_text(json.dumps(plan, indent=2, sort_keys=True) + '\n')
    return plan


def restore_plan(root: Path, directory: Path) -> dict:
    plan = json.loads((directory / 'plan.json').read_text())
    validate_plan(plan)
    if sha256(directory / 'source.bundle') != plan.get('bundle_sha256'):
        raise ValueError('release source bundle checksum mismatch')
    if git(root, 'rev-parse', 'HEAD') != plan['source_commit']:
        raise ValueError('restore must start from the exact pushed source')
    git(root, 'fetch', '--quiet', str(directory / 'source.bundle'), f'refs/tags/{plan["tag"]}:refs/tags/{plan["tag"]}')
    git(root, 'checkout', '--quiet', '--detach', plan['release_commit'])
    check_release_checkout(root, plan)
    return plan


class GitHub:
    def __init__(self, repository: str):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('invalid GitHub repository')
        self.repository = repository

    def api(self, path: str, payload=None, method='GET'):
        args = ['gh', 'api', '--method', method, f'repos/{self.repository}/{path}']
        if payload is not None:
            args += ['--input', '-']
        return json.loads(subprocess.check_output(args, input=json.dumps(payload) if payload is not None else None, text=True))

    def find_release(self, tag: str):
        # Listing includes authenticated drafts, unlike some get-by-tag paths.
        matches, page = [], 1
        while True:
            releases = self.api(f'releases?per_page=100&page={page}')
            matches.extend(release for release in releases if release['tag_name'] == tag)
            if len(releases) < 100:
                break
            page += 1
        if len(matches) > 1:
            raise ValueError('multiple releases use the planned tag')
        return matches[0] if matches else None

    def create_draft(self, plan: dict, body: str):
        # Reference the already-pushed tag. Do not ask the API to create it
        # from a branch or target_commitish containing workflow changes.
        return self.api('releases', {'tag_name': plan['tag'], 'name': 'Wir Schaffen DLC ' + plan['version'],
                        'body': body, 'draft': True, 'prerelease': False, 'make_latest': 'false'}, 'POST')

    def upload(self, tag: str, assets: list[Path]) -> None:
        subprocess.run(['gh', 'release', 'upload', tag, *map(str, assets), '--repo', self.repository, '--clobber'], check=True)

    def get_release(self, release_id: int):
        return self.api(f'releases/{release_id}')

    def publish(self, release_id: int, latest: bool):
        return self.api(f'releases/{release_id}', {'draft': False, 'make_latest': 'true' if latest else 'false'}, 'PATCH')


def release_marker(plan: dict) -> str:
    return f'<!-- {AUTOMATION}:{plan["run_id"]}:{plan["source_commit"]}:{plan["release_commit"]} -->'


def publish_release(root: Path, plan: dict, directory: Path, github: GitHub) -> dict:
    check_release_checkout(root, plan)
    manifest = json.loads((directory / 'release-manifest.json').read_text())
    if manifest.get('tag') != plan['tag'] or manifest.get('version') != plan['version'] or manifest.get('source_commit') != plan['release_commit']:
        raise ValueError('release assets do not match the planned source/version')
    files = sorted(directory.iterdir())
    expected = set(manifest['artifacts']) | {'release-manifest.json', 'SHA256SUMS.txt'}
    if {path.name for path in files} != expected or any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError('unexpected release asset inventory')
    checksums = {}
    for line in (directory / 'SHA256SUMS.txt').read_text().splitlines():
        digest, name = line.split('  ', 1)
        if name in checksums or Path(name).name != name or name not in expected:
            raise ValueError('invalid release checksum inventory')
        checksums[name] = digest
    if set(checksums) != expected - {'SHA256SUMS.txt'} or any(sha256(directory / name) != digest for name, digest in checksums.items()):
        raise ValueError('release asset checksum mismatch')
    for name, record in manifest['artifacts'].items():
        if record['sha256'] != checksums[name]:
            raise ValueError('release manifest checksum mismatch')
    public = manifest.get('public_assets')
    if (not isinstance(public, list) or len(public) != 2 or any(not isinstance(name, str) for name in public)
            or len(set(public)) != 2 or not set(public) <= set(manifest['artifacts'])):
        raise ValueError('release must declare exactly two executable bundles')
    for architecture in ('arm64', 'amd64'):
        pattern = rf'Wir-Schaffen-DLC-{re.escape(plan["version"])}-macos-[0-9]+-{architecture}\.zip'
        if sum(bool(re.fullmatch(pattern, name)) for name in public) != 1:
            raise ValueError('release must declare exactly two executable bundles: arm64 and amd64')
    assets = [directory / name for name in sorted(public)]
    expected_public = set(public)
    marker = release_marker(plan)
    release = github.find_release(plan['tag'])
    if release and marker not in (release.get('body') or ''):
        raise ValueError('refusing to modify a release owned by another run or user')
    if release and not release['draft']:
        if not expected_public <= {asset['name'] for asset in release['assets'] if asset['state'] == 'uploaded'}:
            raise ValueError('published release is missing assets; existing bytes will not be overwritten')
        return release
    # Never force-push a tag. A competing reservation must fail safely.
    git(root, 'push', 'origin', f'refs/tags/{plan["tag"]}:refs/tags/{plan["tag"]}')
    if release is None:
        body = (f'Automatic macOS release for source commit `{plan["source_commit"]}`.\n\n'
                'Download **arm64** for Apple Silicon or **amd64** for Intel. '
                'Each ZIP includes the standalone installer and its double-clickable launcher.\n\n'
                'Both builds passed tests, version, architecture, signature, and checksum checks.\n\n' + marker)
        release = github.create_draft(plan, body)
    if not release['draft'] or release.get('immutable') or marker not in (release.get('body') or ''):
        raise ValueError('assets may only be uploaded to this run\'s mutable draft')
    github.upload(plan['tag'], assets)
    release = github.get_release(release['id'])
    uploaded = {asset['name']: asset for asset in release['assets']}
    if set(uploaded) != expected_public:
        raise ValueError('GitHub draft does not contain the complete verified asset set')
    for asset in assets:
        remote = uploaded[asset.name]
        if remote.get('state') != 'uploaded' or remote.get('size') != asset.stat().st_size or remote.get('digest') != 'sha256:' + sha256(asset):
            raise ValueError('GitHub upload verification failed: ' + asset.name)
    if not release['draft'] or marker not in (release.get('body') or ''):
        raise ValueError('release changed during upload')
    # An older failed run retried later must not displace a newer push's release.
    latest = True
    for tag in git(root, 'tag', '--list').splitlines():
        saved = tag_plan(root, tag)
        if saved and saved['run_number'] > plan['run_number']:
            latest = False
    result = github.publish(release['id'], latest)
    if result.get('draft') or result.get('tag_name') != plan['tag']:
        raise ValueError('GitHub did not publish the expected release')
    return result


def sync_source_branch(root: Path, plan: dict, branch: str, github: GitHub) -> str:
    """Update only release metadata in a clean, disposable CI checkout."""
    validate_plan(plan)
    if branch not in PUBLISH_BRANCHES:
        raise ValueError('refusing to synchronize an unconfigured publishing branch')
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('version synchronization requires a clean checkout')
    if tag_plan(root, plan['tag']) != {key: value for key, value in plan.items() if key != 'bundle_sha256'}:
        raise ValueError('release plan differs from its annotated tag')
    release = github.find_release(plan['tag'])
    if (not release or release.get('draft') or release.get('tag_name') != plan['tag']
            or release_marker(plan) not in (release.get('body') or '')):
        raise ValueError('source versions may only follow this run\'s published release')
    for attempt in range(3):
        git(root, 'fetch', '--quiet', '--no-tags', 'origin', f'refs/heads/{branch}')
        base = git(root, 'rev-parse', 'FETCH_HEAD')
        git(root, 'checkout', '--quiet', '--detach', base)
        if version_tuple(versions(root)[0]) > version_tuple(plan['version']):
            return f'{branch}: retained newer source version'
        stamp_release_files(root, plan['version'])
        changed = set(git(root, 'diff', '--name-only').splitlines())
        if not changed <= set(RELEASE_FILES):
            raise ValueError('version synchronization changed unrelated source files')
        if not changed:
            return f'{branch}: already synchronized with {plan["tag"]}'
        git(root, 'add', '--', *RELEASE_FILES)
        git(root, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '-m',
            f'Synchronize source version and README image with {plan["tag"]} [skip ci]',
            env=bot_environment(root, base))
        try:
            # A normal fast-forward push preserves concurrent user changes.
            git(root, 'push', 'origin', f'HEAD:refs/heads/{branch}')
            return f'{branch}: synchronized with {plan["tag"]}'
        except subprocess.CalledProcessError:
            remote = git(root, 'ls-remote', '--heads', 'origin', f'refs/heads/{branch}').split()
            if attempt == 2 or not remote or remote[0] == base:
                raise
            # The branch advanced: regenerate from its new source next time.
    raise AssertionError('unreachable')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('plan')
    create.add_argument('--source', required=True)
    create.add_argument('--run-id', required=True)
    create.add_argument('--run-number', type=int, required=True)
    create.add_argument('--directory', type=Path, required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--directory', type=Path, required=True)
    publish = commands.add_parser('publish')
    publish.add_argument('--plan', type=Path, required=True)
    publish.add_argument('--assets', type=Path, required=True)
    publish.add_argument('--repository', required=True)
    sync = commands.add_parser('sync')
    sync.add_argument('--plan', type=Path, required=True)
    sync.add_argument('--repository', required=True)
    sync.add_argument('--branch', action='append', choices=PUBLISH_BRANCHES, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'plan':
            plan = make_plan(ROOT, args.source, args.run_id, args.run_number, args.directory)
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as outputs:
                    for name, value in {'source': plan['source_commit'], 'commit': plan['release_commit'], 'tag': plan['tag'], 'version': plan['version']}.items():
                        outputs.write(f'{name}={value}\n')
            print(json.dumps(plan, indent=2))
        elif args.command == 'restore':
            print(json.dumps(restore_plan(ROOT, args.directory), indent=2))
        elif args.command == 'publish':
            plan = json.loads(args.plan.read_text())
            result = publish_release(ROOT, plan, args.assets, GitHub(args.repository))
            print(result['html_url'])
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                    summary.write(f'### WSDLC {plan["tag"]}\n\nPublished arm64 and amd64 downloads: {result["html_url"]}\n\nSource push: `{plan["source_commit"]}`. Release commit: `{plan["release_commit"]}`.\n')
        else:
            plan = json.loads(args.plan.read_text())
            github = GitHub(args.repository)
            for branch in dict.fromkeys(args.branch):
                message = sync_source_branch(ROOT, plan, branch, github)
                print(message)
                if os.environ.get('GITHUB_STEP_SUMMARY'):
                    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                        summary.write(message + '\n\n')
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'error: {error}\n')


if __name__ == '__main__':
    main()
