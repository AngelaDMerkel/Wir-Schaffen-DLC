#!/usr/bin/env python3
"""Verify both architecture outputs and prepare assets for one exact GitHub release."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(directory: Path, tag: str, commit: str, repository: str) -> dict:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]*)", tag):
        raise ValueError("release tag must be v followed by a package version")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("expected a full source commit")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid GitHub repository")
    version = tag[1:]
    expected_python = (ROOT / '.github/release-python-version').read_text().strip()
    expected_lock_digest = digest(ROOT / 'requirements-release.txt')
    pyinstaller_pin = re.search(r'^pyinstaller==([^\s]+)', (ROOT / 'requirements-release.txt').read_text(), re.MULTILINE)
    if pyinstaller_pin is None:
        raise ValueError('missing PyInstaller release pin')
    expected_files: set[str] = set()
    all_artifacts: dict[str, str] = {}
    toolchains = []
    lock_digest = None
    for architecture, label in (("arm64", "arm64"), ("x86_64", "amd64")):
        name = f"build-info-{version}-{label}.json"
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing build metadata: {name}")
        record = json.loads(path.read_text())
        for key, expected in {"schema_version": 1, "tag": tag, "version": version,
                              "source_commit": commit, "architecture": architecture,
                              "asset_architecture": label, "python": expected_python,
                              "pyinstaller": pyinstaller_pin.group(1)}.items():
            if record.get(key) != expected:
                raise ValueError(f"{name}: mismatched {key}")
        if not re.fullmatch(r"[0-9a-f]{64}", record.get("build_requirements_sha256", "")):
            raise ValueError("missing build dependency identity")
        if lock_digest is not None and record['build_requirements_sha256'] != lock_digest:
            raise ValueError("architecture jobs used different build dependencies")
        if record['build_requirements_sha256'] != expected_lock_digest:
            raise ValueError('build dependencies do not match the tagged source lockfile')
        lock_digest = record['build_requirements_sha256']
        artifacts = record.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            raise ValueError(f"{name}: empty artifact inventory")
        native_archives = 0
        for filename, expected_hash in artifacts.items():
            if Path(filename).name != filename or "\\" in filename or any(ord(c) < 32 for c in filename):
                raise ValueError("unsafe artifact filename")
            if filename in expected_files:
                raise ValueError("duplicate artifact across architecture jobs")
            if re.fullmatch(rf"Wir-Schaffen-DLC-{re.escape(version)}-macos-[0-9]+-{label}\.zip", filename):
                native_archives += 1
            elif not (label == 'arm64' and filename in {
                f"civ5_mod_dlc_packer-{version}-py3-none-any.whl", f"civ5_mod_dlc_packer-{version}.tar.gz"
            }):
                raise ValueError(f"unexpected artifact for {label}: {filename}")
            artifact = directory / filename
            if not artifact.is_file() or artifact.is_symlink() or digest(artifact) != expected_hash:
                raise ValueError(f"artifact checksum mismatch: {filename}")
            expected_files.add(filename)
            all_artifacts[filename] = expected_hash
        if native_archives != 1:
            raise ValueError(f"expected exactly one {label} native archive")
        expected_files.add(name)
        all_artifacts[name] = digest(path)
        toolchains.append({key: record[key] for key in ("architecture", "python", "pyinstaller", "macos")})
    python_files = {f"civ5_mod_dlc_packer-{version}-py3-none-any.whl", f"civ5_mod_dlc_packer-{version}.tar.gz"}
    if not python_files <= expected_files:
        raise ValueError("missing Python source/wheel artifacts")
    if {path.name for path in directory.iterdir()} != expected_files:
        raise ValueError("unexpected files in the combined release assets")
    base = f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}"
    manifest = {
        "schema_version": 1, "version": version, "tag": tag, "source_commit": commit,
        "build_requirements_sha256": lock_digest, "toolchains": toolchains,
        "artifacts": {name: {"sha256": sha, "url": f"{base}/{quote(name, safe='')}"}
                      for name, sha in sorted(all_artifacts.items())},
    }
    manifest_path = directory / 'release-manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    all_artifacts[manifest_path.name] = digest(manifest_path)
    (directory / 'SHA256SUMS.txt').write_text(''.join(f'{sha}  {name}\n' for name, sha in sorted(all_artifacts.items())))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--repository', required=True)
    args = parser.parse_args()
    try:
        manifest = prepare(args.directory, args.tag, args.commit, args.repository)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f'error: {error}\n')
    print(f"Verified {manifest['tag']} at {manifest['source_commit']} for arm64 and amd64")
