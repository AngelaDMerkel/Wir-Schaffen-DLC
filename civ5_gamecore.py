"""Verified native GameCore products and recoverable installation transactions.

Only Wir Schaffen DLC owns installation state. No product-specific stock backup
is ever created. Tests inject a catalog and use disposable application bundles.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import uuid
from urllib.parse import urlparse
from urllib.request import urlopen
import zipfile

BINARY = 'libCvGameCoreDLL_Expansion2_DLL.dylib'
BINARY_RELATIVE = 'Contents/MacOS/' + BINARY
DLC_RELATIVE = 'Contents/Assets/Assets/DLC'
ABI = 'aspyr-civ5-bnw-x86_64-10.11.6-v1'
CACHES = ('Civ5CoreDatabase.db', 'Civ5DebugDatabase.db', 'Civ5ModsDatabase.db', 'Localization-Merged.db')
STOCK_HASH = '0da6a5ffc283c3f147b20a7ec426e4ed85a6838ab891faf61b50af4e25c4a09c'
HOST_HASH = 'd56d6bfbc0ef517fcb7cbaff46c42d1bdfab809c084684045761bd9d85807ee9'
PRODUCTS = {
    'lekmod': {'repository': 'https://github.com/AngelaDMerkel/Lekmod', 'payload': 'LEKMOD'},
    'vox-populi': {'repository': 'https://github.com/AngelaDMerkel/Community-Patch-DLL-macOS', 'payload': 'VoxPopuli'},
}
# Release entries are added only after validation and publication authorization.
# Each entry contains product, version, url, and an independently pinned sha256.
RELEASES: dict[str, dict[str, str]] = {}
CATALOG = {
    'products': PRODUCTS, 'stock_hashes': [STOCK_HASH], 'host_hashes': [HOST_HASH],
    'legacy_lekmod': {
        'gamecore_sha256': 'ada65581fbe74cce79801c92690923d0a35fd50d1fcbf093cfcabcfc923fa40a',
        'payload_sha256': '05891fa3318ee398b06c1f5d88f94ad30cef840081e2be95422055c22f2bef22',
    },
}


class GameCoreError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def tree_files(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise GameCoreError(f'unsupported payload entry: {path}')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha256(path)
    return result


def tree_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(''.join(f'{digest}  {name}\n' for name, digest in sorted(files.items())).encode()).hexdigest()


def safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value):
        raise GameCoreError('invalid artifact path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')) or ':' in value:
        raise GameCoreError('unsafe artifact path: ' + value)
    return path


def require_hash(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise GameCoreError('a complete lowercase SHA-256 digest is required')


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def verify_package(directory: Path, catalog: dict = CATALOG) -> dict:
    """Validate all paths and bytes after the archive's external hash passes."""
    try:
        manifest = json.loads((directory / 'manifest.json').read_text())
        product = manifest['product']
        expected = catalog['products'][product]
        if manifest['schema_version'] != 1 or manifest['abi_id'] != ABI:
            raise GameCoreError('unsupported artifact schema or ABI')
        if manifest['source']['repository'] != expected['repository']:
            raise GameCoreError('artifact source repository is not trusted for this product')
        for source in [manifest['source'], manifest['compat']]:
            if not re.fullmatch('[0-9a-f]{40}', source['commit']):
                raise GameCoreError('source provenance must use full commit identifiers')
        if manifest['compat']['repository'] != 'https://github.com/AngelaDMerkel/civ5-macos-gamecore-compat':
            raise GameCoreError('untrusted compatibility source')
        if not isinstance(manifest['version'], str) or not manifest['version']:
            raise GameCoreError('missing product version')
        payload_name = expected['payload']
        destinations = {'gamecore': BINARY_RELATIVE, 'payload': {payload_name: DLC_RELATIVE + '/' + payload_name}}
        if manifest['destinations'] != destinations:
            raise GameCoreError('artifact installation destinations do not match the trusted product')
        if set(manifest['conflicts']) != set(catalog['products']) - {product}:
            raise GameCoreError('artifact conflict declaration is incomplete')
        supported = manifest['supported_stock_game_hashes']
        if not supported['gamecore'] or not supported['executable']:
            raise GameCoreError('artifact has no supported stock game hashes')
        if not set(supported['gamecore']) <= set(catalog['stock_hashes']) or not set(supported['executable']) <= set(catalog['host_hashes']):
            raise GameCoreError('artifact claims unsupported game hashes')
        files = manifest['files']
        if not isinstance(files, dict):
            raise GameCoreError('artifact file inventory must be an object')
        for name, digest in files.items():
            safe_relative(name)
            require_hash(digest)
            if name != BINARY and not name.startswith(('licenses/', 'payload/' + payload_name + '/')):
                raise GameCoreError('unexpected artifact file: ' + name)
        actual = tree_files(directory)
        if set(actual) != set(files) | {'manifest.json', 'SHA256SUMS'}:
            raise GameCoreError('artifact inventory does not cover every file exactly')
        if any(actual[name] != digest for name, digest in files.items()):
            raise GameCoreError('artifact file hash mismatch')
        sums = {}
        for line in (directory / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            safe_relative(name)
            require_hash(digest)
            if name in sums:
                raise GameCoreError('duplicate checksum entry')
            sums[name] = digest
        if sums != {name: digest for name, digest in actual.items() if name != 'SHA256SUMS'}:
            raise GameCoreError('SHA256SUMS verification failed')
        if manifest['gamecore_sha256'] != actual[BINARY]:
            raise GameCoreError('GameCore hash mismatch')
        payload_files = {name: digest for name, digest in files.items() if name.startswith('payload/')}
        if manifest['payload_sha256'] != tree_hash(payload_files):
            raise GameCoreError('payload tree hash mismatch')
        if not list((directory / 'payload' / payload_name).glob('*.Civ5Pkg')):
            raise GameCoreError('payload has no root DLC package declaration')
        if not manifest['licenses'] or any(name not in files or not name.startswith('licenses/') for name in manifest['licenses']):
            raise GameCoreError('missing licensing information')
        return manifest
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise GameCoreError('invalid GameCore artifact: ' + str(error)) from error


@contextmanager
def open_artifact(archive: Path, expected_sha256: str, catalog: dict = CATALOG):
    require_hash(expected_sha256)
    if archive.is_symlink() or not archive.is_file() or sha256(archive) != expected_sha256:
        raise GameCoreError('archive SHA-256 does not match the independent trusted digest')
    with tempfile.TemporaryDirectory(prefix='wir-schaffen-gamecore-') as temporary:
        root = Path(temporary)
        try:
            with zipfile.ZipFile(archive) as source:
                names = set()
                total = 0
                for item in source.infolist():
                    name = item.filename.rstrip('/') if item.is_dir() else item.filename
                    relative = safe_relative(name)
                    key = name.casefold()
                    if key in names:
                        raise GameCoreError('duplicate or case-colliding archive path')
                    names.add(key)
                    mode = item.external_attr >> 16
                    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                        raise GameCoreError('archive contains a link or special file')
                    total += item.file_size
                    if total > 4 * 1024**3 or len(names) > 100000:
                        raise GameCoreError('artifact exceeds extraction limits')
                    destination = root / relative
                    if item.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with source.open(item) as reader, destination.open('xb') as writer:
                            shutil.copyfileobj(reader, writer)
                        destination.chmod(0o755 if mode & 0o111 else 0o644)
            yield root, verify_package(root, catalog)
        except (zipfile.BadZipFile, OSError, ValueError) as error:
            raise GameCoreError('could not read GameCore archive: ' + str(error)) from error


def download_release(product: str, destination: Path) -> tuple[Path, str]:
    release = RELEASES.get(product)
    if not release:
        raise GameCoreError(f'no validated public {product} release is cataloged yet; use a local archive and its independently recorded SHA-256')
    prefix = PRODUCTS[product]['repository'] + '/releases/download/'
    if not release['url'].startswith(prefix):
        raise GameCoreError('release URL is outside the trusted product repository')
    require_hash(release['sha256'])
    with urlopen(release['url'], timeout=60) as response, destination.open('xb') as stream:
        final = urlparse(response.url)
        if final.scheme != 'https' or final.hostname not in {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}:
            raise GameCoreError('release redirected outside GitHub asset hosting')
        total = 0
        while data := response.read(1024 * 1024):
            total += len(data)
            if total > 2 * 1024**3:
                raise GameCoreError('release download exceeds size limit')
            stream.write(data)
    if sha256(destination) != release['sha256']:
        raise GameCoreError('downloaded release failed SHA-256 verification')
    return destination, release['sha256']


class ProductManager:
    def __init__(self, game_app: Path, user_data: Path, catalog: dict = CATALOG):
        # Resolve the app identity, then reject symlinks along every managed path.
        if game_app.is_symlink() or user_data.is_symlink():
            raise GameCoreError('application and user-data paths must not be symlinks')
        self.app = game_app.resolve()
        self.user_data = user_data.resolve()
        self.catalog = catalog
        app_id = hashlib.sha256(str(self.app).encode()).hexdigest()[:24]
        self.root = self.user_data / 'WirSchaffenDLC' / 'GameCore' / app_id
        self.state_path = self.root / 'state.json'
        self.backup = self.root / 'stock.dylib'
        self.transaction = self.root / 'transaction'
        self.binary = self.app / BINARY_RELATIVE
        self._safe(self.binary)
        self._safe(self.root)

    def _names(self) -> list[str]:
        return ['binary', *self.catalog['products'], *('cache-' + name for name in CACHES), 'state']

    def _safe(self, path: Path) -> None:
        for candidate in [path, *path.parents]:
            if candidate.is_symlink():
                raise GameCoreError('refusing symlink in managed path: ' + str(candidate))

    def _state(self) -> dict | None:
        self._safe(self.state_path)
        if not self.state_path.exists():
            return None
        try:
            state = json.loads(self.state_path.read_text())
            if state['schema_version'] != 1 or state['app'] != str(self.app) or state['product'] not in {'stock', *self.catalog['products']}:
                raise ValueError('invalid state identity')
            if state['stock_sha256'] not in self.catalog['stock_hashes']:
                raise ValueError('unsupported recorded stock hash')
            require_hash(state['gamecore_sha256'])
            if state['product'] != 'stock':
                require_hash(state['payload_sha256'])
            elif state['gamecore_sha256'] != state['stock_sha256']:
                raise ValueError('stock state must identify the verified original')
            return state
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise GameCoreError('invalid installation state: ' + str(error)) from error

    def _payload_path(self, product: str) -> Path:
        path = self.app / DLC_RELATIVE / self.catalog['products'][product]['payload']
        self._safe(path)
        return path

    def _payload_digest(self, product: str) -> str | None:
        path = self._payload_path(product)
        if not path.exists():
            return None
        if not path.is_dir():
            raise GameCoreError('payload destination is not a directory')
        return tree_hash(tree_files(path))

    def status(self) -> dict:
        self._safe(self.transaction)
        if self.transaction.exists():
            return {'status': 'interrupted', 'recovery_required': True, 'state_directory': str(self.root)}
        if not self.binary.is_file():
            raise GameCoreError('missing GameCore library')
        host = self.app / 'Contents/MacOS/Civilization V'
        self._safe(host)
        if not host.is_file() or sha256(host) not in self.catalog['host_hashes']:
            return {'status': 'unsupported-host', 'product': None}
        digest = sha256(self.binary)
        state = self._state()
        if state:
            if self.backup.is_symlink() or not self.backup.is_file() or sha256(self.backup) != state['stock_sha256']:
                raise GameCoreError('canonical stock backup is missing or corrupt')
            product = state['product']
            if product != 'stock' and self._payload_digest(product) != state['payload_sha256']:
                return {'status': 'modified-payload', 'product': product}
            if digest == state['gamecore_sha256']:
                return {'status': 'managed', 'product': product, 'version': state.get('version'), 'gamecore_sha256': digest}
            if digest in self.catalog['stock_hashes']:
                return {'status': 'steam-restored', 'product': product, 'gamecore_sha256': digest}
            return {'status': 'unknown-binary', 'product': product, 'gamecore_sha256': digest}
        if digest in self.catalog['stock_hashes']:
            return {'status': 'stock', 'product': 'stock', 'gamecore_sha256': digest}
        legacy = self.catalog.get('legacy_lekmod', {})
        if digest == legacy.get('gamecore_sha256') and self._payload_digest('lekmod') == legacy.get('payload_sha256'):
            return {'status': 'legacy-lekmod', 'product': 'lekmod', 'gamecore_sha256': digest}
        return {'status': 'unknown-binary', 'product': None, 'gamecore_sha256': digest}

    def plan(self, product: str, manifest: dict | None = None) -> dict:
        if product not in {'stock', *self.catalog['products']}:
            raise GameCoreError('unknown product')
        if product == 'stock' and manifest is not None:
            raise GameCoreError('stock restoration does not accept a custom artifact')
        current = self.status()
        if current['status'] not in {'managed', 'steam-restored', 'stock', 'legacy-lekmod'}:
            raise GameCoreError('installation requires attention: ' + current['status'])
        active = current['product']
        for other in self.catalog['products']:
            existing = self._payload_path(other)
            if existing.exists() and other != active:
                raise GameCoreError('refusing an unowned/conflicting payload: ' + str(existing))
        for name in CACHES:
            cache = self._target('cache-' + name)
            if cache.exists() and not cache.is_file():
                raise GameCoreError('unexpected database cache path: ' + str(cache))
        state = self._state()
        stock_source = self.backup if self.backup.exists() else self.binary
        if current['status'] == 'legacy-lekmod' and not self.backup.exists():
            stock_source = self.binary.with_name(BINARY + '.lekmod-original')
        self._safe(stock_source)
        if not stock_source.is_file() or sha256(stock_source) not in self.catalog['stock_hashes']:
            raise GameCoreError('a verified stock GameCore is required; a custom binary will never be backed up as stock')
        stock_digest = sha256(stock_source)
        if product != 'stock':
            if not manifest or manifest['product'] != product:
                raise GameCoreError('a verified matching artifact is required')
            supported = manifest['supported_stock_game_hashes']
            if stock_digest not in supported['gamecore'] or sha256(self.app / 'Contents/MacOS/Civilization V') not in supported['executable']:
                raise GameCoreError('artifact does not support this stock game build')
        return {'from': active, 'to': product, 'current_status': current['status'],
                'stock_source': str(stock_source), 'stock_sha256': stock_digest,
                'state_directory': str(self.root), 'version': manifest['version'] if manifest else None}

    @contextmanager
    def _lock(self):
        self._safe(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / 'lock'
        self._safe(lock)
        with lock.open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise GameCoreError('another GameCore transaction is running') from error
            yield

    def _target(self, name: str) -> Path:
        if name == 'binary':
            result = self.binary
        elif name == 'state':
            result = self.state_path
        elif name in self.catalog['products']:
            result = self._payload_path(name)
        elif name.startswith('cache-') and name[6:] in CACHES:
            result = self.user_data / 'cache' / name[6:]
        else:
            raise GameCoreError('invalid journal target')
        self._safe(result)
        return result

    def _replace(self, destination: Path, source: Path | None) -> None:
        self._safe(destination)
        if source is None:
            if destination.is_dir():
                shutil.rmtree(destination)
            elif destination.exists():
                destination.unlink()
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            incoming = Path(tempfile.mkdtemp(prefix='.wir-gamecore-', dir=destination.parent))
            try:
                shutil.copytree(source, incoming, dirs_exist_ok=True)
                if destination.exists():
                    shutil.rmtree(destination)
                incoming.replace(destination)
            finally:
                if incoming.exists():
                    shutil.rmtree(incoming)
        else:
            fd, name = tempfile.mkstemp(prefix='.wir-gamecore-', dir=destination.parent)
            os.close(fd)
            temporary = Path(name)
            try:
                shutil.copy2(source, temporary)
                with temporary.open('rb') as stream:
                    os.fsync(stream.fileno())
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)

    def _recover_locked(self) -> None:
        self._safe(self.transaction)
        if not self.transaction.exists():
            return
        journal_path = self.transaction / 'journal.json'
        self._safe(journal_path)
        if not journal_path.exists():
            # Preparation writes the journal before any managed destination changes.
            shutil.rmtree(self.transaction)
            return
        journal = json.loads(journal_path.read_text())
        if journal.get('app') != str(self.app) or set(journal.get('before', {})) != set(self._names()):
            raise GameCoreError('invalid transaction journal')
        host = self.app / 'Contents/MacOS/Civilization V'
        self._safe(host)
        if not host.is_file() or sha256(host) != journal.get('host_sha256'):
            raise GameCoreError('host changed during the transaction; refusing stale GameCore recovery')
        # Verify every backup before restoring any destination.
        for name, before in journal['before'].items():
            source = self.transaction / 'before' / name
            self._safe(source)
            if before is not None:
                actual = tree_hash(tree_files(source)) if source.is_dir() else sha256(source)
                if actual != before:
                    raise GameCoreError('transaction recovery backup failed verification')
        for name, before in journal['before'].items():
            self._replace(self._target(name), self.transaction / 'before' / name if before is not None else None)
        self._finish_transaction()

    def _finish_transaction(self) -> None:
        completed = self.root / ('completed-' + uuid.uuid4().hex)
        self.transaction.rename(completed)
        shutil.rmtree(completed)

    def recover(self) -> None:
        with self._lock():
            self._recover_locked()

    def switch(self, product: str, package: Path | None = None, dry_run: bool = False) -> dict:
        manifest = verify_package(package, self.catalog) if package else None
        if dry_run:
            return self.plan(product, manifest)
        with self._lock():
            plan = self.plan(product, manifest)
            if not self.backup.exists():
                self._replace(self.backup, Path(plan['stock_source']))
                if sha256(self.backup) != plan['stock_sha256']:
                    raise GameCoreError('canonical stock backup failed verification')
            self.transaction.mkdir()
            before_dir = self.transaction / 'before'
            before_dir.mkdir()
            journal = {'app': str(self.app), 'host_sha256': sha256(self.app / 'Contents/MacOS/Civilization V'), 'before': {}}
            for name in self._names():
                target = self._target(name)
                if target.exists():
                    snapshot = before_dir / name
                    if target.is_dir():
                        shutil.copytree(target, snapshot)
                        journal['before'][name] = tree_hash(tree_files(snapshot))
                    else:
                        shutil.copy2(target, snapshot)
                        journal['before'][name] = sha256(snapshot)
                else:
                    journal['before'][name] = None
            write_json(self.transaction / 'journal.json', journal)
            try:
                for other in self.catalog['products']:
                    payload = package / 'payload' / self.catalog['products'][other]['payload'] if other == product and package else None
                    self._replace(self._payload_path(other), payload)
                incoming_binary = package / BINARY if package else self.backup
                self._replace(self.binary, incoming_binary)
                state = {'schema_version': 1, 'app': str(self.app), 'product': product,
                         'version': plan['version'], 'stock_sha256': plan['stock_sha256'],
                         'gamecore_sha256': manifest['gamecore_sha256'] if manifest else plan['stock_sha256'],
                         'payload_sha256': self._payload_digest(product) if product != 'stock' else None,
                         'manifest': manifest}
                if sha256(self.binary) != state['gamecore_sha256']:
                    raise GameCoreError('installed GameCore failed verification')
                if manifest:
                    prefix = 'payload/' + self.catalog['products'][product]['payload'] + '/'
                    expected = {name[len(prefix):]: digest for name, digest in manifest['files'].items() if name.startswith(prefix)}
                    if tree_files(self._payload_path(product)) != expected:
                        raise GameCoreError('installed payload failed verification')
                for name in CACHES:
                    self._replace(self._target('cache-' + name), None)
                write_json(self.state_path, state)
            except BaseException:
                self._recover_locked()
                raise
            self._finish_transaction()
            return self.status()
