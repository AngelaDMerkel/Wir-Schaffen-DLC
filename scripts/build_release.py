#!/usr/bin/env python3
"""Build official source and thin arm64/x86_64 macOS release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import civ5_dlc_packer as packer


VERSION = packer.VERSION
MACOS_VERSION = platform.mac_ver()[0]
MACOS_MAJOR = MACOS_VERSION.split(".", 1)[0]
RELEASE_DIR = ROOT / "release"
WORK_DIR = ROOT / ".release-build"
SUPPORTED_ARCHITECTURES = ("arm64", "x86_64")


@dataclass(frozen=True)
class Toolchain:
    architecture: str
    python: Path

    def command(self, *arguments: str) -> list[str]:
        return ["/usr/bin/arch", f"-{self.architecture}", str(self.python), *arguments]


def run(command: Sequence[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(map(str, command)), flush=True)
    subprocess.run(command, cwd=cwd, check=True, env=env)


def output(command: Sequence[str], cwd: Path = ROOT) -> str:
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("pyproject.toml has no project version")
    return match.group(1)


def validate_toolchain(toolchain: Toolchain) -> None:
    if toolchain.architecture not in SUPPORTED_ARCHITECTURES:
        raise RuntimeError(f"unsupported release architecture: {toolchain.architecture}")
    if not toolchain.python.is_file():
        raise RuntimeError(f"Python interpreter does not exist: {toolchain.python}")
    actual = output(toolchain.command("-c", "import platform; print(platform.machine())"))
    if actual != toolchain.architecture:
        raise RuntimeError(
            f"{toolchain.python} ran as {actual}, expected {toolchain.architecture}"
        )
    output(toolchain.command("-m", "PyInstaller", "--version"))


def verify_native_binary(binary: Path, architecture: str) -> None:
    architectures = output(["/usr/bin/lipo", "-archs", str(binary)]).split()
    if architectures != [architecture]:
        raise RuntimeError(
            f"native binary architecture mismatch: expected {architecture}, got {architectures}"
        )
    version = output([str(binary), "--version"])
    if version != f"wir-schaffen-dlc {VERSION}":
        raise RuntimeError(f"standalone binary version check failed: {version}")


def write_native_bundle(binary: Path, toolchain: Toolchain) -> tuple[Path, Path]:
    architecture = toolchain.architecture
    label = f"Wir-Schaffen-DLC-{VERSION}-macos-{MACOS_MAJOR}-{architecture}"
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

    pyinstaller_version = output(toolchain.command("-m", "PyInstaller", "--version"))
    python_version = output(
        toolchain.command("-c", "import platform; print(platform.python_version())")
    )
    target = (
        "Apple Silicon Macs"
        if architecture == "arm64"
        else "Intel Macs and Rosetta-capable Apple Silicon Macs"
    )
    (bundle / "README.txt").write_text(
        f"""AngelaDMerkel's: Wir Schaffen DLC {VERSION}

This standalone terminal installer targets {target}. It was built on macOS
{MACOS_VERSION} and does not require a separate Python installation.

1. Quit Civilization V.
2. Double-click "Wir Schaffen DLC.command".
3. Choose installed mods/maps, the exclusive Very Best Mods download, or the
   Excogitare custom-size and geometry patch.
4. Review the packages and confirm installation.

Choose "Restore stock Civilization V" to remove authenticated Wir Schaffen
DLC packages and restore the verified original executable.

The Very Best Mods option requires an internet connection while it verifies
and downloads its seven authoritative Workshop sources.

This build is ad-hoc signed, not Apple-notarized. If macOS blocks the first
launch, Control-click the .command file, choose Open, and confirm Open.

Every multiplayer participant needs byte-identical generated DLC packages and
the same Civilization V expansion/DLC configuration.

Build architecture: {architecture}
Build macOS: {MACOS_VERSION}
Build Python: {python_version}
PyInstaller: {pyinstaller_version}
""",
        encoding="utf-8",
    )

    archive = RELEASE_DIR / f"{label}.zip"
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as output_zip:
        for path in sorted(bundle.rglob("*")):
            output_zip.write(path, Path(bundle.name) / path.relative_to(bundle))
    return bundle, archive


def build_native(toolchain: Toolchain) -> tuple[Path, Path]:
    architecture = toolchain.architecture
    run(toolchain.command("-m", "unittest", "discover", "-s", "tests", "-v"))

    dist = WORK_DIR / f"pyinstaller-dist-{architecture}"
    work = WORK_DIR / f"pyinstaller-work-{architecture}"
    spec = WORK_DIR / f"pyinstaller-spec-{architecture}"
    environment = os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"] = str(
        WORK_DIR / f"pyinstaller-cache-{architecture}"
    )
    run(
        toolchain.command(
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--onefile",
            "--noupx",
            "--target-architecture",
            architecture,
            "--name",
            "wir-schaffen-dlc",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            "--specpath",
            str(spec),
            str(ROOT / "civ5_dlc_installer.py"),
        ),
        env=environment,
    )
    binary = dist / "wir-schaffen-dlc"
    verify_native_binary(binary, architecture)
    return write_native_bundle(binary, toolchain)


def build_python_distributions(toolchain: Toolchain) -> None:
    python_dist = WORK_DIR / "python-dist"
    run(
        toolchain.command(
            "-m", "build", "--no-isolation", "--outdir", str(python_dist)
        )
    )
    for artifact in python_dist.iterdir():
        shutil.copy2(artifact, RELEASE_DIR / artifact.name)


def write_checksums() -> Path:
    artifacts = sorted(
        path
        for path in RELEASE_DIR.iterdir()
        if path.is_file() and path.name != "SHA256SUMS.txt"
    )
    lines = []
    for path in artifacts:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    checksums = RELEASE_DIR / "SHA256SUMS.txt"
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checksums


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--architectures",
        nargs="+",
        choices=SUPPORTED_ARCHITECTURES,
        default=[platform.machine().lower()],
        help="thin native binaries to build",
    )
    parser.add_argument(
        "--arm-python",
        type=Path,
        default=Path(sys.executable),
        help="Python with PyInstaller available for the arm64 build",
    )
    parser.add_argument(
        "--x86-python",
        type=Path,
        help="Python with PyInstaller available under Rosetta for the x86_64 build",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if project_version() != VERSION:
        raise RuntimeError(f"version mismatch: pyproject.toml != {VERSION}")
    if sys.platform != "darwin":
        raise RuntimeError("the native release must be built on macOS")
    if not MACOS_MAJOR:
        raise RuntimeError("could not determine the macOS release")

    requested = list(dict.fromkeys(args.architectures))
    python_by_architecture = {
        "arm64": args.arm_python,
        "x86_64": args.x86_python,
    }
    toolchains: list[Toolchain] = []
    for architecture in requested:
        python = python_by_architecture[architecture]
        if python is None:
            raise RuntimeError(f"a Python toolchain is required for {architecture}")
        # Preserve virtual-environment symlinks: resolving them to the base
        # interpreter discards pyvenv.cfg and its installed build tools.
        python_path = Path(os.path.abspath(python.expanduser()))
        toolchain = Toolchain(architecture, python_path)
        validate_toolchain(toolchain)
        toolchains.append(toolchain)

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    shutil.rmtree(RELEASE_DIR, ignore_errors=True)
    WORK_DIR.mkdir()
    RELEASE_DIR.mkdir()

    build_python_distributions(toolchains[0])
    native_artifacts = [build_native(toolchain) for toolchain in toolchains]
    checksums = write_checksums()

    print(f"\nBuilt release {VERSION}:")
    for bundle, archive in native_artifacts:
        print(f"  Native bundle: {bundle}")
        print(f"  Native archive: {archive}")
    print(f"  Checksums: {checksums}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
