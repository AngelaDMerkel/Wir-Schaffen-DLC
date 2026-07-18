#!/usr/bin/env python3
"""Interactive macOS installer for Civ V mods packaged as DLC."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import civ5_dlc_packer as packer


GAME_APP_NAME = "Civilization V.app"
GAME_STEAM_PATH = Path("steamapps/common/Sid Meier's Civilization V") / GAME_APP_NAME
USER_DATA_PATH = Path("Library/Application Support/Sid Meier's Civilization 5")
BRAND_NAME = "AngelaDMerkel's: Wir Schaffen DLC"


class InstallerError(RuntimeError):
    pass


def terminal_banner(color: bool = False) -> str:
    reset = "\033[0m" if color else ""
    bold = "\033[1m" if color else ""
    red = "\033[31m" if color else ""
    gold = "\033[33m" if color else ""
    plain_title = "AngelaDMerkel's: WIR SCHAFFEN DLC"
    title = f"{bold}AngelaDMerkel's:{reset} {bold}WIR {red}SCHAFFEN {gold}DLC{reset}"
    subtitle = f"Civilization V multiplayer mod installer v{packer.VERSION}"
    return "\n".join(
        (
            "╭────────────────────────────────────────────────────╮",
            f"│  {title}{' ' * (50 - len(plain_title))}│",
            f"│  {subtitle}{' ' * (50 - len(subtitle))}│",
            "╰────────────────────────────────────────────────────╯",
        )
    )


def terminal_supports_color() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


@dataclass(frozen=True)
class Civ5Install:
    game_app: Path
    user_data: Path
    game_assets: Path
    dlc_dir: Path
    mods_dir: Path
    base_db: Path | None


@dataclass(frozen=True)
class InstalledMod:
    path: Path
    manifest: packer.Manifest


@dataclass(frozen=True)
class PreparedPackage:
    mod: InstalledMod
    path: Path
    install_name: str
    report: packer.BuildReport


def unique_existing_dirs(paths: Sequence[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        expanded = path.expanduser()
        if not expanded.is_dir():
            continue
        key = os.path.normcase(str(expanded.resolve()))
        if key not in seen:
            seen.add(key)
            result.append(expanded.resolve())
    return result


def steam_library_paths(home: Path) -> list[Path]:
    steam = home / "Library/Application Support/Steam"
    libraries = [steam]
    config = steam / "steamapps/libraryfolders.vdf"
    if config.is_file():
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for match in re.finditer(r'^\s*"path"\s*"([^"]+)"', text, flags=re.MULTILINE):
            libraries.append(Path(match.group(1).replace("\\\\", "\\")))
    return unique_existing_dirs(libraries)


def discover_game_apps(home: Path | None = None) -> list[Path]:
    home = (home or Path.home()).expanduser()
    candidates = [
        home / "Library/Application Support/Steam" / GAME_STEAM_PATH,
        Path("/Applications") / GAME_APP_NAME,
        home / "Applications" / GAME_APP_NAME,
    ]
    candidates.extend(library / GAME_STEAM_PATH for library in steam_library_paths(home))
    return [path for path in unique_existing_dirs(candidates) if game_assets_path(path).is_dir()]


def discover_user_data_dirs(home: Path | None = None) -> list[Path]:
    home = (home or Path.home()).expanduser()
    return [path for path in unique_existing_dirs([home / USER_DATA_PATH]) if (path / "MODS").is_dir()]


def game_assets_path(game_app: Path) -> Path:
    return game_app / "Contents/Assets/Assets"


def validate_install(game_app: Path, user_data: Path) -> Civ5Install:
    game_app = game_app.expanduser().resolve()
    user_data = user_data.expanduser().resolve()
    game_assets = game_assets_path(game_app)
    dlc_dir = game_assets / "DLC"
    mods_dir = user_data / "MODS"
    missing = [path for path in (game_assets, dlc_dir, mods_dir) if not path.is_dir()]
    if missing:
        raise InstallerError("invalid Civilization V installation; missing " + ", ".join(map(str, missing)))
    database = user_data / "cache/Civ5CoreDatabase.db"
    return Civ5Install(
        game_app=game_app,
        user_data=user_data,
        game_assets=game_assets,
        dlc_dir=dlc_dir,
        mods_dir=mods_dir,
        base_db=database if database.is_file() else None,
    )


def discover_mods(mods_dir: Path) -> tuple[list[InstalledMod], list[str]]:
    mods: list[InstalledMod] = []
    skipped: list[str] = []
    for path in sorted((item for item in mods_dir.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
        try:
            manifest = packer.parse_manifest(path)
        except (packer.PackError, ET.ParseError, OSError, ValueError) as exc:
            skipped.append(f"{path.name}: {exc}")
            continue
        mods.append(InstalledMod(path=path, manifest=manifest))
    mods.sort(key=lambda mod: (mod.manifest.name.casefold(), mod.manifest.version, mod.path.name.casefold()))
    return mods, skipped


def parse_selection(value: str, count: int) -> list[int]:
    value = value.strip().lower()
    if value == "all":
        return list(range(count))
    if not value:
        raise ValueError("no mods selected")
    selected: set[int] = set()
    for token in re.split(r"[\s,]+", value):
        if not token:
            continue
        if token.isdigit():
            start = end = int(token)
        else:
            match = re.fullmatch(r"(\d+)-(\d+)", token)
            if not match:
                raise ValueError(f"invalid selection: {token}")
            start, end = map(int, match.groups())
            if start > end:
                raise ValueError(f"range must be ascending: {token}")
        if start < 1 or end > count:
            raise ValueError(f"selection is outside 1-{count}: {token}")
        selected.update(range(start - 1, end))
    if not selected:
        raise ValueError("no mods selected")
    return sorted(selected)


def choose_path(
    label: str,
    paths: Sequence[Path],
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> Path:
    if not paths:
        raise InstallerError(f"could not find {label}")
    if len(paths) == 1:
        return paths[0]
    output_fn(f"\nFound more than one {label}:")
    for number, path in enumerate(paths, 1):
        output_fn(f"  {number}. {path}")
    while True:
        try:
            indexes = parse_selection(input_fn(f"Choose {label} [1-{len(paths)}]: "), len(paths))
        except ValueError as exc:
            output_fn(f"  {exc}")
            continue
        if len(indexes) != 1:
            output_fn("  Choose exactly one.")
            continue
        return paths[indexes[0]]


def choose_mods(
    mods: Sequence[InstalledMod],
    selection: str | None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> list[InstalledMod]:
    if not mods:
        raise InstallerError("no valid ModBuddy mods were found")
    output_fn("\nInstalled mods:")
    for number, mod in enumerate(mods, 1):
        flags = []
        if mod.manifest.supports_multiplayer is False:
            flags.append("declares no multiplayer support")
        if mod.manifest.supports_mac is False:
            flags.append("declares no Mac support")
        suffix = f"  [{'; '.join(flags)}]" if flags else ""
        output_fn(f"  {number:>2}. {mod.manifest.name} (v {mod.manifest.version}){suffix}")
    prompt = "\nSelect mods (for example 1,3-5 or all; q to quit): "
    value = selection
    while True:
        if value is None:
            value = input_fn(prompt)
        if value.strip().lower() in {"q", "quit"}:
            return []
        try:
            indexes = parse_selection(value, len(mods))
        except ValueError as exc:
            if selection is not None:
                raise InstallerError(str(exc)) from exc
            output_fn(f"  {exc}")
            value = None
            continue
        return [mods[index] for index in indexes]


def package_install_name(mod: InstalledMod) -> str:
    safe_name = re.sub(r"[/:\\\x00-\x1f]", "-", mod.manifest.name).strip(" .")
    safe_name = re.sub(r"\s+", " ", safe_name) or "Unnamed Mod"
    safe_name = safe_name[:140].rstrip()
    return f"Civ5MP - {safe_name} (v {mod.manifest.version}) [{mod.manifest.mod_id[:8]}]"


def prepare_packages(
    mods: Sequence[InstalledMod],
    install: Civ5Install,
    staging_dir: Path,
    output_fn: Callable[[str], None] = print,
) -> list[PreparedPackage]:
    prepared: list[PreparedPackage] = []
    for position, mod in enumerate(mods, 1):
        install_name = package_install_name(mod)
        output_path = staging_dir / install_name
        output_fn(f"\n→ [{position}/{len(mods)}] Packaging {mod.manifest.name}...")
        args = argparse.Namespace(
            mod=mod.path,
            output=output_path,
            base_db=install.base_db,
            game_assets=install.game_assets,
            ui_set="Expansion2",
            priority=10,
            force=False,
        )
        report = packer.pack(args)
        for warning in report.warnings:
            output_fn(f"  ! {warning}")
        prepared.append(PreparedPackage(mod=mod, path=output_path, install_name=install_name, report=report))
        output_fn("  ✓ Ready.")
    return prepared


def _remove_directory(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise InstallerError(f"refusing to remove unexpected path: {path}")
        shutil.rmtree(path)


def install_packages(packages: Sequence[PreparedPackage], dlc_dir: Path, replace: bool) -> list[Path]:
    dlc_dir = dlc_dir.resolve()
    if not dlc_dir.is_dir():
        raise InstallerError(f"DLC directory does not exist: {dlc_dir}")
    if not os.access(dlc_dir, os.W_OK):
        raise InstallerError(f"DLC directory is not writable: {dlc_dir}")

    unsafe_names = [
        package.install_name
        for package in packages
        if package.install_name in {"", ".", ".."} or Path(package.install_name).name != package.install_name
    ]
    if unsafe_names:
        raise InstallerError("refusing unsafe package name: " + ", ".join(unsafe_names))
    destinations = [dlc_dir / package.install_name for package in packages]
    if len({path.name for path in destinations}) != len(destinations):
        raise InstallerError("the selection contains duplicate mod packages")
    existing = [path for path in destinations if path.exists()]
    if existing and not replace:
        raise InstallerError("already installed (use --replace to update): " + ", ".join(path.name for path in existing))
    unsafe_existing = [path for path in existing if not path.is_dir() or path.is_symlink()]
    if unsafe_existing:
        raise InstallerError("refusing to replace unexpected DLC path: " + ", ".join(map(str, unsafe_existing)))

    transaction = uuid.uuid4().hex
    changes: list[tuple[Path, Path | None]] = []
    incoming_paths: list[Path] = []
    try:
        for index, (package, destination) in enumerate(zip(packages, destinations)):
            incoming = dlc_dir / f".civ5-mod-dlc-incoming-{transaction}-{index}"
            backup = dlc_dir / f".civ5-mod-dlc-backup-{transaction}-{index}" if destination.exists() else None
            incoming_paths.append(incoming)
            shutil.copytree(package.path, incoming)
            if backup is not None:
                destination.rename(backup)
            try:
                incoming.rename(destination)
            except Exception:
                if backup is not None and backup.exists():
                    backup.rename(destination)
                raise
            changes.append((destination, backup))
    except Exception:
        for destination, backup in reversed(changes):
            _remove_directory(destination)
            if backup is not None and backup.exists():
                backup.rename(destination)
        for incoming in incoming_paths:
            _remove_directory(incoming)
        raise
    for _, backup in changes:
        if backup is not None:
            _remove_directory(backup)
    return destinations


def yes(value: str) -> bool:
    return value.strip().lower() in {"y", "yes"}


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {packer.VERSION}")
    parser.add_argument("--game-app", type=Path, help="path to Civilization V.app")
    parser.add_argument("--user-data", type=Path, help="path containing the MODS and cache directories")
    parser.add_argument("--select", help="non-interactive mod selection, such as 1,3-5 or all")
    parser.add_argument("--yes", action="store_true", help="install without the final confirmation")
    parser.add_argument("--replace", action="store_true", help="replace matching packages installed by this tool")
    parser.add_argument("--dry-run", action="store_true", help="package and validate, but do not install")
    return parser


def run(
    args: argparse.Namespace,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    output_fn(terminal_banner(color=terminal_supports_color() and output_fn is print))

    game_apps = [args.game_app] if args.game_app else discover_game_apps()
    user_dirs = [args.user_data] if args.user_data else discover_user_data_dirs()
    game_app = choose_path("Civilization V application", game_apps, input_fn, output_fn)
    user_data = choose_path("Civilization V user-data directory", user_dirs, input_fn, output_fn)
    install = validate_install(game_app, user_data)
    output_fn(f"\nGame: {install.game_app}")
    output_fn(f"Mods: {install.mods_dir}")
    output_fn(f"DLC:  {install.dlc_dir}")
    if install.base_db is None:
        output_fn("! No Civ5CoreDatabase.db cache was found; mods needing SQL compatibility will fail.")

    mods, skipped = discover_mods(install.mods_dir)
    selected = choose_mods(mods, args.select, input_fn, output_fn)
    if not selected:
        output_fn("No changes made.")
        return 0
    if skipped:
        output_fn(f"\nSkipped {len(skipped)} invalid mod director{'y' if len(skipped) == 1 else 'ies'}.")

    output_fn("\nQuit Civilization V before continuing.")
    with tempfile.TemporaryDirectory(prefix="civ5-mod-dlc-") as temp:
        prepared = prepare_packages(selected, install, Path(temp), output_fn)
        if args.dry_run:
            output_fn(f"\nDry run complete: {len(prepared)} mod(s) packaged and validated; nothing installed.")
            return 0

        existing = [install.dlc_dir / package.install_name for package in prepared]
        existing = [path for path in existing if path.exists()]
        replace = args.replace
        if existing and not replace:
            output_fn("\nThese packages are already installed:")
            for path in existing:
                output_fn(f"  {path.name}")
            if args.yes or not yes(input_fn("Replace them? [y/N]: ")):
                if args.yes:
                    raise InstallerError("matching packages already exist; pass --replace to update them")
                output_fn("No changes made.")
                return 0
            replace = True

        if not args.yes:
            if not yes(input_fn(f"\nInstall {len(prepared)} package(s) into the DLC folder? [y/N]: ")):
                output_fn("No changes made.")
                return 0
        destinations = install_packages(prepared, install.dlc_dir, replace=replace)

    output_fn("\n✓ Installed successfully:")
    for destination in destinations:
        output_fn(f"  {destination.name}")
    output_fn("\nEvery multiplayer participant must install the same generated packages.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (InstallerError, packer.PackError, ET.ParseError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
