#!/usr/bin/env python3
"""Build the official source and native macOS release artifacts."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import civ5_dlc_packer as packer


VERSION = packer.VERSION
ARCHITECTURE = platform.machine().lower()
MACOS_MAJOR = platform.mac_ver()[0].split(".", 1)[0]
RELEASE_DIR = ROOT / "release"
WORK_DIR = ROOT / ".release-build"


def run(*command: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True, env=env)


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("pyproject.toml has no project version")
    return match.group(1)


def write_native_bundle(binary: Path) -> tuple[Path, Path]:
    label = f"Wir-Schaffen-DLC-{VERSION}-macos-{MACOS_MAJOR}-{ARCHITECTURE}"
    bundle = RELEASE_DIR / label
    bundle.mkdir()
    installed_binary = bundle / "wir-schaffen-dlc"
    shutil.copy2(binary, installed_binary)

    launcher = bundle / "Wir Schaffen DLC.command"
    launcher.write_text(
        "#!/bin/sh\n"
        "RELEASE_DIR=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
        "exec \"$RELEASE_DIR/wir-schaffen-dlc\" \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    shutil.copy2(ROOT / "LICENSE", bundle / "LICENSE")
    shutil.copy2(ROOT / "NOTICE", bundle / "NOTICE")
    shutil.copy2(ROOT / "CHANGELOG.md", bundle / "CHANGELOG.md")

    pyinstaller_version = subprocess.check_output(
        [sys.executable, "-m", "PyInstaller", "--version"], text=True
    ).strip()
    (bundle / "README.txt").write_text(
        f"""AngelaDMerkel's: Wir Schaffen DLC {VERSION}

This standalone terminal installer is for Apple Silicon Macs running macOS
{MACOS_MAJOR} or newer. It does not require a separate Python installation.

1. Quit Civilization V.
2. Double-click \"Wir Schaffen DLC.command\".
3. Select mods by number and confirm installation.

This build is ad-hoc signed, not Apple-notarized. If macOS blocks the first
launch, Control-click the .command file, choose Open, and confirm Open.

Every multiplayer participant needs byte-identical generated DLC packages and
the same Civilization V expansion/DLC configuration.

Build architecture: {ARCHITECTURE}
Build macOS: {platform.mac_ver()[0]}
Build Python: {platform.python_version()}
PyInstaller: {pyinstaller_version}
""",
        encoding="utf-8",
    )

    archive = RELEASE_DIR / f"{label}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(bundle.rglob("*")):
            output.write(path, Path(bundle.name) / path.relative_to(bundle))
    return bundle, archive


def write_checksums() -> Path:
    artifacts = sorted(path for path in RELEASE_DIR.iterdir() if path.is_file() and path.name != "SHA256SUMS.txt")
    lines = []
    for path in artifacts:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    checksums = RELEASE_DIR / "SHA256SUMS.txt"
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checksums


def main() -> int:
    if project_version() != VERSION:
        raise RuntimeError(f"version mismatch: pyproject.toml != {VERSION}")
    if sys.platform != "darwin":
        raise RuntimeError("the native release must be built on macOS")
    if not MACOS_MAJOR:
        raise RuntimeError("could not determine the macOS release")

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    shutil.rmtree(RELEASE_DIR, ignore_errors=True)
    WORK_DIR.mkdir()
    RELEASE_DIR.mkdir()

    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")

    python_dist = WORK_DIR / "python-dist"
    run(sys.executable, "-m", "build", "--no-isolation", "--outdir", str(python_dist))
    for artifact in python_dist.iterdir():
        shutil.copy2(artifact, RELEASE_DIR / artifact.name)

    pyinstaller_dist = WORK_DIR / "pyinstaller-dist"
    pyinstaller_environment = os.environ.copy()
    pyinstaller_environment["PYINSTALLER_CONFIG_DIR"] = str(WORK_DIR / "pyinstaller-cache")
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--noupx",
        "--name",
        "wir-schaffen-dlc",
        "--distpath",
        str(pyinstaller_dist),
        "--workpath",
        str(WORK_DIR / "pyinstaller-work"),
        "--specpath",
        str(WORK_DIR),
        str(ROOT / "civ5_dlc_installer.py"),
        env=pyinstaller_environment,
    )
    binary = pyinstaller_dist / "wir-schaffen-dlc"
    bundle, archive = write_native_bundle(binary)
    checksums = write_checksums()

    print(f"\nBuilt release {VERSION}:")
    print(f"  Native bundle: {bundle}")
    print(f"  Native archive: {archive}")
    print(f"  Checksums: {checksums}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
