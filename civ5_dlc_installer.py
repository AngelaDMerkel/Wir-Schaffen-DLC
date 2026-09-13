#!/usr/bin/env python3
"""Interactive macOS installer for Civ V mods packaged as DLC."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - the interactive installer targets macOS
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

import civ5_dlc_packer as packer
import civ5_best_mods as best_mods
import civ5_gamecore as gamecore


GAME_APP_NAME = "Civilization V.app"
GAME_STEAM_PATH = Path("steamapps/common/Sid Meier's Civilization V") / GAME_APP_NAME
USER_DATA_PATH = Path("Library/Application Support/Sid Meier's Civilization 5")
BRAND_NAME = "AngelaDMerkel's: Wir Schaffen DLC"
SHARED_UI_BRIDGE_ID = str(uuid.uuid5(packer.PACKER_NAMESPACE, "wir-schaffen-dlc:shared-ui-bridge"))
EXCOGITARE_PATCH_ID = str(
    uuid.uuid5(packer.PACKER_NAMESPACE, "wir-schaffen-dlc:excogitare-map-patch")
)
EXCOGITARE_PATCH_NAME = "AngelaDMerkel's Excogitare Map Patch"
EXCOGITARE_WORLD_SIZES = (
    ("WORLDSIZE_EXTREME", 180, 94, 20, 20),
    ("WORLDSIZE_COLOSSAL", 170, 110, 22, 22),
)
EXCOGITARE_CUSTOM_WORLD_TYPES = frozenset(item[0] for item in EXCOGITARE_WORLD_SIZES)
RESTORE_STOCK_NAME = "Restore stock Civilization V"
STOCK_REBUILD_CACHE_NAMES = (
    "Civ5CoreDatabase.db",
    "Civ5DebugDatabase.db",
    "Civ5ModsDatabase.db",
    "Localization-Merged.db",
)
ENGINE_RELATIVE_PATH = Path("Contents/MacOS/Civilization V")
ENGINE_ORIGINAL_SHA256 = "d56d6bfbc0ef517fcb7cbaff46c42d1bdfab809c084684045761bd9d85807ee9"
ENGINE_DIVISOR_ONLY_SHA256 = "7005e95aebe7755e6923ca25fe866394621efbf715b394f8cc2b05161e6f6563"
ENGINE_ZERO_SPAN_SHA256 = "282aac32f1d7c0e22290111dec1a2cb0b6aedbb7a29ac3081f6973b168374082"
ENGINE_BOUNDED_FALLBACK_SHA256 = "546ef9cc452faf10eb9700d9ceabda55991b351a73357ee9fcdefb54596536f9"
ENGINE_PATCH_OFFSET = 0x988F7E
ENGINE_ORIGINAL_BYTES = bytes.fromhex("31d289d8f7f74189c585ff0f84a4010000")
# Move the existing divisor-zero test ahead of divl. This preserves the
# routine's nonzero path byte-for-byte and follows its existing zero return.
ENGINE_PATCHED_BYTES = bytes.fromhex("85ff0f84ad01000031d289d8f7f74189c5")
ENGINE_SPAN_PATCH_OFFSET = 0x98A5DC
ENGINE_SPAN_ORIGINAL_BYTES = bytes.fromhex(
    "31c90f2ec3721831c966662e0f1f840000000000"
    "f30f5cc2ffc90f2ec373f5488d7b080f2ec87617"
    "6666662e0f1f840000000000f30f58c2ffc10f2ec877f5"
)
# An earlier field-test guard only skipped zero/unordered spans. Retain its
# exact signature so a test build can be upgraded safely in place.
ENGINE_ZERO_SPAN_PREFIX = bytes.fromhex(
    "0f2ed9763a31c90f2ec372130f1f840000000000"
)
ENGINE_ZERO_SPAN_BYTES = (
    ENGINE_ZERO_SPAN_PREFIX + ENGINE_SPAN_ORIGINAL_BYTES[len(ENGINE_ZERO_SPAN_PREFIX) :]
)
# A field-test build clamped an unrepresentable coordinate before calling the
# downstream geometry routine. Retain its exact signature so it can be
# upgraded; that clamp can propagate an invalid FFastVector index.
ENGINE_BOUNDED_FALLBACK_BYTES = bytes.fromhex(
    "31c90f2ed976380f28e0f30f5ce1f30f5ee2660f3a0ae401"
    "f3440f2cd44181fa00000080740ff30f59e2f30f5cc44489d1"
    "f7d9eb0a0f28c131c90f1f440000"
)
# Replace the unbounded add/subtract loops with the equivalent constant-time
# floor/modulo calculation. Degenerate, unordered, or unrepresentable inputs
# return through the routine's existing canonical no-result sentinel path.
ENGINE_SPAN_PATCHED_BYTES = bytes.fromhex(
    "31c90f2ed9762e0f28e0f30f5ce1f30f5ee2660f3a0ae401"
    "f3440f2cd44181fa00000080740ff30f59e2f30f5cc44489d1"
    "f7d9eb0a31ff4989feeb6f0f1f00"
)
ENGINE_PATCH_SITES = (
    (ENGINE_PATCH_OFFSET, ENGINE_ORIGINAL_BYTES, ENGINE_PATCHED_BYTES),
    (ENGINE_SPAN_PATCH_OFFSET, ENGINE_SPAN_ORIGINAL_BYTES, ENGINE_SPAN_PATCHED_BYTES),
)
ENGINE_BACKUP_DIR = "Wir Schaffen DLC Backups"
ENGINE_BACKUP_NAME = f"Civilization V.{ENGINE_ORIGINAL_SHA256[:16]}.original"


class InstallerError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def engine_patch_state(executable: Path) -> str:
    with executable.open("rb") as source:
        source.seek(ENGINE_PATCH_OFFSET)
        divisor = source.read(len(ENGINE_ORIGINAL_BYTES))
        source.seek(ENGINE_SPAN_PATCH_OFFSET)
        span = source.read(len(ENGINE_SPAN_ORIGINAL_BYTES))
    if divisor == ENGINE_ORIGINAL_BYTES and span == ENGINE_SPAN_ORIGINAL_BYTES:
        return "original"
    if divisor == ENGINE_PATCHED_BYTES and span == ENGINE_SPAN_ORIGINAL_BYTES:
        return "divisor_guard"
    if divisor == ENGINE_PATCHED_BYTES and span == ENGINE_ZERO_SPAN_BYTES:
        return "zero_span_guard"
    if divisor == ENGINE_PATCHED_BYTES and span == ENGINE_BOUNDED_FALLBACK_BYTES:
        return "bounded_fallback_guard"
    if divisor == ENGINE_PATCHED_BYTES and span == ENGINE_SPAN_PATCHED_BYTES:
        return "patched"
    return "unsupported"


def engine_backup_path(user_data: Path) -> Path:
    return user_data / ENGINE_BACKUP_DIR / ENGINE_BACKUP_NAME


def apply_colossal_engine_patch(game_app: Path, user_data: Path) -> bool:
    executable = game_app / ENGINE_RELATIVE_PATH
    if not executable.is_file():
        raise InstallerError(f"Civilization V executable is missing: {executable}")
    state = engine_patch_state(executable)
    if state == "patched":
        return False
    actual_hash = file_sha256(executable)
    supported_original = state == "original" and actual_hash == ENGINE_ORIGINAL_SHA256
    supported_divisor_guard = (
        state == "divisor_guard" and actual_hash == ENGINE_DIVISOR_ONLY_SHA256
    )
    supported_zero_span_guard = (
        state == "zero_span_guard" and actual_hash == ENGINE_ZERO_SPAN_SHA256
    )
    supported_bounded_fallback = (
        state == "bounded_fallback_guard"
        and actual_hash == ENGINE_BOUNDED_FALLBACK_SHA256
    )
    if not (
        supported_original
        or supported_divisor_guard
        or supported_zero_span_guard
        or supported_bounded_fallback
    ):
        raise InstallerError(
            "the Excogitare map engine guard does not support this Civilization V executable "
            f"(SHA-256 {actual_hash})"
        )

    backup = engine_backup_path(user_data)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        if not backup.is_file() or file_sha256(backup) != ENGINE_ORIGINAL_SHA256:
            raise InstallerError(f"refusing to overwrite an invalid engine backup: {backup}")
    else:
        shutil.copy2(executable, backup)
        if file_sha256(backup) != ENGINE_ORIGINAL_SHA256:
            raise InstallerError("the Civilization V engine backup failed verification")

    temporary = executable.with_name(f".{executable.name}.wir-schaffen-dlc-{uuid.uuid4().hex}")
    try:
        shutil.copy2(executable, temporary)
        with temporary.open("r+b") as target:
            for offset, original, patched in ENGINE_PATCH_SITES:
                target.seek(offset)
                current = target.read(len(original))
                if current == patched:
                    continue
                accepted = (original,)
                if offset == ENGINE_SPAN_PATCH_OFFSET:
                    accepted += (ENGINE_ZERO_SPAN_BYTES, ENGINE_BOUNDED_FALLBACK_BYTES)
                if current not in accepted:
                    raise InstallerError("the executable changed while applying the engine guard")
                target.seek(offset)
                target.write(patched)
            target.flush()
            os.fsync(target.fileno())
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", str(temporary)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--verbose=2", str(temporary)],
            check=True,
            capture_output=True,
            text=True,
        )
        if engine_patch_state(temporary) != "patched":
            raise InstallerError("the signed executable does not contain the engine guard")
        os.replace(temporary, executable)
    except subprocess.CalledProcessError as exc:
        raise InstallerError(f"could not ad-hoc sign the patched Civilization V executable: {exc.stderr.strip()}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def restore_colossal_engine_patch(game_app: Path, user_data: Path) -> bool:
    executable = game_app / ENGINE_RELATIVE_PATH
    if engine_patch_state(executable) == "original":
        return False
    backup = engine_backup_path(user_data)
    if not backup.is_file() or file_sha256(backup) != ENGINE_ORIGINAL_SHA256:
        raise InstallerError(f"a valid original executable backup is unavailable: {backup}")
    temporary = executable.with_name(f".{executable.name}.wir-schaffen-dlc-restore-{uuid.uuid4().hex}")
    try:
        shutil.copy2(backup, temporary)
        os.replace(temporary, executable)
    finally:
        if temporary.exists():
            temporary.unlink()
    if engine_patch_state(executable) != "original" or file_sha256(executable) != ENGINE_ORIGINAL_SHA256:
        raise InstallerError("the restored Civilization V executable failed verification")
    return True


BAUHAUS_WORDMARK_LINES = (
    "ANGELADMERKEL'S:",
    " __      _____ ___",
    " \\ \\    / /_ _| _ \\",
    "  \\ \\/\\/ / | ||   /",
    "   \\_/\\_/ |___|_|_\\",
    "  ___  ___ _  _   _   ___ ___ ___ _  _",
    " / __|/ __| || | /_\\ | __| __| __| \\| |",
    " \\__ \\ (__| __ |/ _ \\| _|| _|| _|| .` |",
    " |___/\\___|_||_/_/ \\_\\_| |_| |___|_|\\_|",
    "       ___  _    ___",
    "      |   \\| |  / __|",
    "      | |) | |_| (__",
    "      |___/|____\\___|",
)

# Fixed 24-bit Bauhaus light palette. The warm parchment surface is the exact
# gentle-yellow token from the approved mockup; the other colors use that
# mockup's Bauhaus light-theme values. The full-screen renderer reapplies the
# base surface after every reset so Terminal.app's profile cannot leak through.
TUI_BACKGROUND = "\033[48;2;251;244;221m"
TUI_FOREGROUND = "\033[38;2;31;38;60m"
TUI_MUTED = "\033[38;2;86;97;127m"
TUI_YELLOW = "\033[38;2;140;105;0m"
TUI_YELLOW_BACKGROUND = "\033[48;2;255;221;69m"
TUI_SELECTED_FOREGROUND = "\033[38;2;21;23;32m"
TUI_CYAN = "\033[38;2;33;111;130m"
TUI_RED = "\033[38;2;214;75;64m"
TUI_RESET = "\033[0m"
TUI_SET_DEFAULT_BACKGROUND = "\033]11;rgb:fb/f4/dd\007"
TUI_RESET_DEFAULT_BACKGROUND = "\033]111\007"
TUI_CONTROL_SEQUENCE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)


def terminal_display_width(value: str) -> int:
    plain = TUI_CONTROL_SEQUENCE_RE.sub("", value)
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in plain
    )


def terminal_canvas_width() -> int:
    return max(32, min(88, shutil.get_terminal_size(fallback=(80, 30)).columns - 4))


def centered_terminal_line(value: str, width: int) -> str:
    return " " * max(0, (width - len(value)) // 2) + value


def centered_wordmark_lines(width: int) -> list[str]:
    result: list[str] = []
    for start, end in ((0, 1), (1, 5), (5, 9), (9, 13)):
        group = BAUHAUS_WORDMARK_LINES[start:end]
        group_width = max(map(len, group))
        prefix = " " * max(0, (width - group_width) // 2)
        result.extend(prefix + line for line in group)
    return result


def terminal_columns(left: str, right: str, width: int) -> str:
    gap = max(2, width - len(left) - len(right))
    return (left + " " * gap + right)[:width]


def terminal_banner(color: bool = False) -> str:
    reset = TUI_RESET if color else ""
    bold = "\033[1m" if color else ""
    yellow = TUI_YELLOW if color else ""
    dim = TUI_MUTED if color else ""
    title = f"\033]0;AngelaDMerkel's: Wir Schaffen DLC {packer.VERSION}\007" if color else ""
    width = terminal_canvas_width()
    wordmark: list[str] = []
    for index, centered in enumerate(centered_wordmark_lines(width)):
        if index == 0:
            wordmark.append(f"{title}{bold}{yellow}{centered}{reset}")
        else:
            wordmark.append(f"{yellow}{centered}{reset}")
    tagline = centered_terminal_line("Fine, I'll do it for you.", width)
    subtitle = centered_terminal_line(
        f"Civilization V multiplayer mod installer · v{packer.VERSION}", width
    )
    return "\n".join(
        (
            *wordmark,
            "",
            f"{bold}{tagline}{reset}",
            f"{dim}{subtitle}{reset}",
        )
    )


def installation_mode_menu(
    color: bool = False,
    selected: int = 0,
    full_screen: bool = False,
) -> str:
    if selected not in {0, 1, 2, 3}:
        raise ValueError(f"invalid installation-mode selection: {selected}")
    reset = TUI_RESET if color else ""
    bold = "\033[1m" if color else ""
    red = TUI_RED if color else ""
    cyan = TUI_CYAN if color else ""
    dim = TUI_MUTED if color else ""
    width = terminal_canvas_width()
    descriptions = (
        "Download seven verified mods from authoritative remote sources"
        if width >= 68
        else "Download seven verified remote mods",
        "Manual payload selection from this Civ V installation"
        if width >= 68
        else "Select from local MODS and Maps",
        "Excogitare sizes and custom-geometry engine compatibility"
        if width >= 68
        else "Extreme/Colossal sizes and geometry guard",
        "Remove Wir Schaffen DLC and restore the verified original game"
        if width >= 68
        else "Remove generated DLC and restore Civ V",
    )

    def option_lines(
        index: int,
        title: str,
        description: str,
        metadata: str,
        metadata_color: str,
    ) -> tuple[str, str]:
        active = selected == index
        marker = "▶" if active else " "
        title_line = terminal_columns(
            f"{marker} {index + 1:02d}  {title}", metadata, width
        )
        description_line = f"      {description}"
        if active and color:
            return (
                f"\033[1m{TUI_SELECTED_FOREGROUND}{TUI_YELLOW_BACKGROUND}"
                f"{title_line.ljust(width)}{reset}",
                f"{TUI_SELECTED_FOREGROUND}{TUI_YELLOW_BACKGROUND}"
                f"{description_line.ljust(width)}{reset}",
            )
        if active:
            return title_line, description_line
        if color:
            prefix = title_line[: -len(metadata)] if metadata else title_line
            return (
                f"{bold}{prefix}{reset}{metadata_color}{metadata}{reset}",
                f"{dim}{description_line}{reset}",
            )
        return title_line, description_line

    best_title, best_description = option_lines(
        0,
        best_mods.PRESET_NAME,
        descriptions[0],
        "REMOTE",
        red,
    )
    local_title, local_description = option_lines(
        1,
        "Package installed mods & maps",
        descriptions[1],
        "LOCAL",
        cyan,
    )
    patch_title, patch_description = option_lines(
        2,
        "Install AngelaDMerkel's map patch",
        descriptions[2],
        "PATCH",
        red,
    )
    restore_title, restore_description = option_lines(
        3,
        RESTORE_STOCK_NAME,
        descriptions[3],
        "REVERT",
        red,
    )
    hints = (
        "[↑/↓] move   [ENTER] launch   [Q/ESC] abort"
        if full_screen
        else "[1/2/3/4] select   [ENTER] launch 01   [Q] abort"
    )
    options = (
        (best_title, best_description),
        (local_title, local_description),
        (patch_title, patch_description),
        (restore_title, restore_description),
    )
    lines = [f"{bold}SELECT PROGRAM{reset}", ""]
    for index, (title_line, description_line) in enumerate(options):
        lines.append(title_line)
        if not full_screen or index == selected:
            lines.append(description_line)
        lines.append("")
    lines.append(f"{dim}{hints}{reset}")
    return "\n".join(lines)


def terminal_supports_color() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def terminal_supports_full_screen(
    input_fn: Callable[[str], str], output_fn: Callable[[str], None]
) -> bool:
    return (
        termios is not None
        and tty is not None
        and input_fn is input
        and output_fn is print
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def decode_terminal_key(data: bytes) -> str | None:
    if data in {b"\x1b[A", b"\x1bOA", b"k", b"K"}:
        return "up"
    if data in {b"\x1b[B", b"\x1bOB", b"j", b"J"}:
        return "down"
    if data in {b"\r", b"\n"}:
        return "select"
    if data == b"1":
        return "first"
    if data == b"2":
        return "second"
    if data == b"3":
        return "third"
    if data == b"4":
        return "fourth"
    if data in {b"q", b"Q", b"\x03", b"\x1b"}:
        return "quit"
    return None


def read_terminal_key(file_descriptor: int) -> str | None:
    first = os.read(file_descriptor, 1)
    if not first:
        return "quit"
    if first != b"\x1b":
        return decode_terminal_key(first)
    sequence = first
    for _ in range(2):
        ready, _, _ = select.select([file_descriptor], [], [], 0.03)
        if not ready:
            break
        sequence += os.read(file_descriptor, 1)
    return decode_terminal_key(sequence)


@contextlib.contextmanager
def alternate_terminal_screen(color: bool = False) -> Iterator[int]:
    if termios is None or tty is None:
        raise OSError("full-screen terminal support is unavailable")
    file_descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(file_descriptor)
    try:
        tty.setcbreak(file_descriptor, termios.TCSANOW)
        theme = TUI_SET_DEFAULT_BACKGROUND if color else ""
        sys.stdout.write(f"\033[?1049h\033[?25l\033[?7l{theme}")
        sys.stdout.flush()
        yield file_descriptor
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSANOW, original)
        restore_theme = TUI_RESET_DEFAULT_BACKGROUND if color else ""
        sys.stdout.write(f"\033[0m\033[?7h\033[?25h\033[?1049l{restore_theme}")
        sys.stdout.flush()


def compact_terminal_banner(color: bool = False) -> str:
    reset = TUI_RESET if color else ""
    bold = "\033[1m" if color else ""
    yellow = TUI_YELLOW if color else ""
    dim = TUI_MUTED if color else ""
    width = terminal_canvas_width()
    tagline = centered_terminal_line("Fine, I'll do it for you.", width)
    version = centered_terminal_line(f"v{packer.VERSION}", width)
    return "\n".join(
        (
            f"{bold}{yellow}{centered_terminal_line(BRAND_NAME, width)}{reset}",
            f"{bold}{tagline}{reset}",
            f"{dim}{version}{reset}",
        )
    )


def paint_terminal_rows(
    rows: Sequence[str],
    size: os.terminal_size,
    color: bool,
) -> str:
    physical_rows = list(rows[: size.lines])
    physical_rows.extend("" for _ in range(size.lines - len(physical_rows)))
    if not color:
        return "\033[2J\033[H" + "\n".join(physical_rows)

    base_surface = TUI_BACKGROUND + TUI_FOREGROUND
    themed_rows: list[str] = []
    for row in physical_rows:
        # Every local style reset must return to the product surface rather
        # than the user's Terminal profile. Write real space cells across the
        # physical row: Terminal.app's erase-to-end command uses the profile's
        # default background and otherwise leaves visible rectangular tiles.
        row = row.replace(TUI_RESET, TUI_RESET + base_surface)
        padding = " " * max(0, size.columns - terminal_display_width(row))
        themed_rows.append(base_surface + row + base_surface + padding)
    return base_surface + "\033[2J\033[H" + "\n".join(themed_rows)


def full_screen_mode_frame(selected: int, color: bool) -> str:
    size = shutil.get_terminal_size(fallback=(80, 30))
    banner = (
        terminal_banner(color)
        if size.lines >= 28 and size.columns >= 60
        else compact_terminal_banner(color)
    )
    menu = installation_mode_menu(color, selected=selected, full_screen=True)
    body = f"{banner}\n\n{menu}"
    body_lines = body.splitlines()
    top_padding = max(0, (size.lines - len(body_lines)) // 2)
    left_padding = " " * max(0, (size.columns - terminal_canvas_width()) // 2)
    rows = [""] * top_padding
    rows.extend(left_padding + line if line else "" for line in body_lines)
    return paint_terminal_rows(rows, size, color)


PROGRAM_LABELS = {
    "very-best-mods": "PROGRAM 01 · ANGELADMERKEL'S VERY BEST MODS",
    "installed": "PROGRAM 02 · PACKAGE INSTALLED MODS & MAPS",
    "excogitare-patch": "PROGRAM 03 · ANGELADMERKEL'S MAP PATCH",
    "restore-stock": "PROGRAM 04 · RESTORE STOCK CIVILIZATION V",
}


@dataclass
class TerminalProgress:
    phase: str
    item: str
    position: int
    total_items: int
    completed: int = 0
    total: int = 0
    bytes_per_second: float | None = None
    detail: str = ""
    byte_progress: bool = False

    @property
    def fraction(self) -> float:
        if self.total > 0:
            return max(0.0, min(1.0, self.completed / self.total))
        if self.total_items > 0:
            return max(0.0, min(1.0, self.position / self.total_items))
        return 0.0


class FullScreenTerminalUI:
    def __init__(self, color: bool) -> None:
        self.color = color
        self.file_descriptor: int | None = None
        self.program = "SETUP · LOCATE CIVILIZATION V"
        self.status = "ACTIVE"
        self.messages: list[str] = []
        self.prompt_text: str | None = None
        self.input_buffer = ""
        self.progress: TerminalProgress | None = None
        self._screen = None
        self._last_size: os.terminal_size | None = None

    def __enter__(self) -> FullScreenTerminalUI:
        self._screen = alternate_terminal_screen(color=self.color)
        self.file_descriptor = self._screen.__enter__()
        self.redraw(force=True)
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._screen is None:
            return False
        return bool(self._screen.__exit__(exc_type, exc, traceback))

    def set_program(self, mode: str) -> None:
        self.program = PROGRAM_LABELS.get(mode, mode.replace("-", " ").upper())
        self.status = "ACTIVE"
        self.messages.clear()
        self.prompt_text = None
        self.input_buffer = ""
        self.progress = None
        self.redraw(force=True)

    @staticmethod
    def _human_bytes(value: float) -> str:
        units = ("B", "KiB", "MiB", "GiB")
        amount = float(value)
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
            amount /= 1024
        return f"{amount:.1f} GiB"

    def set_progress(
        self,
        phase: str,
        item: str,
        position: int,
        total_items: int,
        completed: int = 0,
        total: int = 0,
        bytes_per_second: float | None = None,
        detail: str = "",
        byte_progress: bool = False,
    ) -> None:
        self.progress = TerminalProgress(
            phase=phase.upper(),
            item=item,
            position=position,
            total_items=total_items,
            completed=completed,
            total=total,
            bytes_per_second=bytes_per_second,
            detail=detail,
            byte_progress=byte_progress,
        )
        self.redraw(force=True)

    def download_progress(self, progress: best_mods.DownloadProgress) -> None:
        self.set_progress(
            progress.phase,
            progress.title,
            progress.position,
            progress.total_items,
            progress.completed_bytes,
            progress.total_bytes,
            progress.bytes_per_second,
            progress.detail,
            byte_progress=progress.total_bytes > 0,
        )

    def package_progress(
        self,
        position: int,
        total_items: int,
        title: str,
        complete: bool,
    ) -> None:
        self.set_progress(
            "PACKAGE",
            title,
            position,
            total_items,
            1 if complete else 0,
            1,
            detail="Package validated" if complete else "Compiling and validating DLC",
        )

    def install_progress(
        self,
        position: int,
        total_items: int,
        title: str,
        complete: bool,
    ) -> None:
        self.set_progress(
            "INSTALL",
            title,
            position,
            total_items,
            1 if complete else 0,
            1,
            detail="Installed" if complete else "Copying transactionally into Civ V",
        )

    def _wrapped_messages(self, width: int) -> list[str]:
        wrapped: list[str] = []
        for message in self.messages:
            if not message:
                wrapped.append("")
                continue
            leading = len(message) - len(message.lstrip(" "))
            prefix = " " * min(leading, max(0, width - 1))
            content = message.lstrip(" ")
            wrapped.extend(
                textwrap.wrap(
                    content,
                    width=max(8, width - len(prefix)),
                    initial_indent=prefix,
                    subsequent_indent=prefix + ("  " if content[:1] in {"•", "!", "✓", "→", "✗"} else ""),
                    replace_whitespace=False,
                    drop_whitespace=True,
                )
                or [prefix]
            )
        return wrapped

    def _style_log_line(self, line: str) -> str:
        if not self.color or not line:
            return line
        stripped = line.lstrip()
        if stripped.startswith(("!", "✗", "error:", "ERROR:")):
            return f"{TUI_RED}{line}{TUI_RESET}"
        if stripped.startswith(("✓",)):
            return f"{TUI_CYAN}{line}{TUI_RESET}"
        if stripped.startswith(("→",)):
            return f"{TUI_YELLOW}{line}{TUI_RESET}"
        return line

    def _progress_panel(self, width: int) -> list[str]:
        if self.progress is None:
            return []
        progress = self.progress
        phases = (
            (("REMOVE", "REMOVE DLC"), ("RESTORE", "RESTORE ENGINE"), ("CACHE", "REBUILD CACHE"))
            if progress.phase in {"REMOVE", "RESTORE", "CACHE"}
            else (("DOWNLOAD", "DOWNLOAD"), ("VERIFY", "VERIFY"), ("PACKAGE", "PACKAGE"), ("INSTALL", "INSTALL"))
        )
        phase_parts: list[str] = []
        for phase, phase_label in phases:
            label = f"[{phase_label}]" if phase == progress.phase else phase_label
            if self.color and phase == progress.phase:
                phase_parts.append(f"\033[1m{TUI_YELLOW}{label}{TUI_RESET}")
            elif self.color:
                phase_parts.append(f"{TUI_MUTED}{label}{TUI_RESET}")
            else:
                phase_parts.append(label)
        pipeline = "PIPELINE  " + "  ".join(phase_parts)
        counter = (
            f"{progress.position}/{progress.total_items}"
            if progress.total_items > 0 and progress.position > 0
            else f"0/{progress.total_items}"
            if progress.total_items > 0
            else ""
        )
        current = terminal_columns(
            f"CURRENT   {progress.item}",
            counter,
            width,
        )
        bar_width = max(10, min(34, width - 16))
        filled = round(bar_width * progress.fraction)
        bar = "█" * filled + "░" * (bar_width - filled)
        percent = round(progress.fraction * 100)
        meter = f"PROGRESS  [{bar}] {percent:>3}%"
        metrics: list[str] = []
        if progress.byte_progress and progress.total > 0:
            metrics.append(
                f"{self._human_bytes(progress.completed)} / {self._human_bytes(progress.total)}"
            )
        if progress.bytes_per_second:
            metrics.append(f"{self._human_bytes(progress.bytes_per_second)}/s")
        detail = progress.detail
        if metrics:
            detail = f"{detail} · {' · '.join(metrics)}" if detail else " · ".join(metrics)
        return [pipeline, current, meter[:width], f"STATUS    {detail}"[:width], ""]

    def frame(self) -> str:
        size = shutil.get_terminal_size(fallback=(80, 30))
        width = terminal_canvas_width()
        left_padding = " " * max(0, (size.columns - width) // 2)
        reset = TUI_RESET if self.color else ""
        bold = "\033[1m" if self.color else ""
        yellow = TUI_YELLOW if self.color else ""
        muted = TUI_MUTED if self.color else ""
        selected_foreground = TUI_SELECTED_FOREGROUND if self.color else ""
        selected_background = TUI_YELLOW_BACKGROUND if self.color else ""
        title = centered_terminal_line(f"{BRAND_NAME} · v{packer.VERSION}", width)
        tagline = centered_terminal_line("Fine, I'll do it for you.", width)
        program_line = terminal_columns(self.program, self.status, width).ljust(width)
        rows = [
            f"{left_padding}{bold}{yellow}{title}{reset}",
            f"{left_padding}{muted}{tagline}{reset}",
            "",
            (
                f"{left_padding}{bold}{selected_foreground}{selected_background}"
                f"{program_line}{reset}"
            ),
            "",
        ]
        rows.extend(
            f"{left_padding}{line}" if line else ""
            for line in self._progress_panel(width)
        )

        footer: list[str] = []
        if self.prompt_text is not None:
            prompt = f"{self.prompt_text} {self.input_buffer}█".strip()
            if len(prompt) > width:
                prompt = prompt[-width:]
            footer = [
                "",
                (
                    f"{left_padding}{selected_foreground}{selected_background}"
                    f"{prompt.ljust(width)}{reset}"
                    if self.color
                    else f"{left_padding}{prompt}"
                ),
                f"{left_padding}{muted}[ENTER] submit   [CTRL-U] clear   [ESC] cancel{reset}",
            ]
        elif self.status in {"COMPLETE", "FAILED", "CANCELLED"}:
            footer = [
                "",
                f"{left_padding}{muted}[ENTER/Q] close Wir Schaffen DLC{reset}",
            ]

        available = max(0, size.lines - len(rows) - len(footer))
        logs = self._wrapped_messages(width)[-available:]
        rows.extend(
            f"{left_padding}{self._style_log_line(line)}" if line else ""
            for line in logs
        )
        rows.extend("" for _ in range(available - len(logs)))
        rows.extend(footer)
        return paint_terminal_rows(rows, size, self.color)

    def redraw(self, force: bool = False) -> None:
        size = shutil.get_terminal_size(fallback=(80, 30))
        if not force and size == self._last_size:
            return
        sys.stdout.write(self.frame())
        sys.stdout.flush()
        self._last_size = size

    def output(self, message: str = "") -> None:
        self.messages.extend(str(message).split("\n"))
        self.messages = self.messages[-500:]
        self.redraw(force=True)

    def input(self, prompt: str) -> str:
        if self.file_descriptor is None:
            raise OSError("the full-screen terminal session is not active")
        self.prompt_text = " ".join(prompt.strip().split())
        self.input_buffer = ""
        self.redraw(force=True)
        while True:
            size = shutil.get_terminal_size(fallback=(80, 30))
            if size != self._last_size:
                self.redraw(force=True)
            ready, _, _ = select.select([self.file_descriptor], [], [], 0.15)
            if not ready:
                continue
            data = os.read(self.file_descriptor, 1)
            if data in {b"\r", b"\n"}:
                answer = self.input_buffer
                self.messages.append(f"{self.prompt_text} {answer}".rstrip())
                self.prompt_text = None
                self.input_buffer = ""
                self.redraw(force=True)
                return answer
            if data in {b"\x03"}:
                raise KeyboardInterrupt
            if data == b"\x1b":
                sequence = data
                for _ in range(2):
                    sequence_ready, _, _ = select.select(
                        [self.file_descriptor], [], [], 0.03
                    )
                    if not sequence_ready:
                        break
                    sequence += os.read(self.file_descriptor, 1)
                if decode_terminal_key(sequence) in {"up", "down"}:
                    continue
                self.prompt_text = None
                self.input_buffer = ""
                self.redraw(force=True)
                return "q"
            if data in {b"\x7f", b"\x08"}:
                self.input_buffer = self.input_buffer[:-1]
            elif data == b"\x15":
                self.input_buffer = ""
            elif 32 <= data[0] < 127:
                self.input_buffer += data.decode("ascii")
            self.redraw(force=True)

    def choose_install_mode(self) -> str:
        if self.file_descriptor is None:
            raise OSError("the full-screen terminal session is not active")
        selected = 0
        last_size: os.terminal_size | None = None
        while True:
            size = shutil.get_terminal_size(fallback=(80, 30))
            if size != last_size:
                sys.stdout.write(full_screen_mode_frame(selected, self.color))
                sys.stdout.flush()
                last_size = size
            ready, _, _ = select.select([self.file_descriptor], [], [], 0.15)
            if not ready:
                continue
            key = read_terminal_key(self.file_descriptor)
            if key == "up":
                selected = (selected - 1) % 4
                last_size = None
            elif key == "down":
                selected = (selected + 1) % 4
                last_size = None
            elif key in {"first", "second", "third", "fourth", "select"}:
                if key == "first":
                    selected = 0
                elif key == "second":
                    selected = 1
                elif key == "third":
                    selected = 2
                elif key == "fourth":
                    selected = 3
                mode = (
                    "very-best-mods",
                    "installed",
                    "excogitare-patch",
                    "restore-stock",
                )[selected]
                self.set_program(mode)
                return mode
            elif key == "quit":
                return "quit"

    def finish(self, status: str) -> None:
        self.status = status
        self.prompt_text = None
        self.input_buffer = ""
        self.redraw(force=True)

    def wait_for_close(self) -> None:
        if self.file_descriptor is None:
            return
        while True:
            size = shutil.get_terminal_size(fallback=(80, 30))
            if size != self._last_size:
                self.redraw(force=True)
            ready, _, _ = select.select([self.file_descriptor], [], [], 0.15)
            if not ready:
                continue
            if read_terminal_key(self.file_descriptor) in {"select", "quit"}:
                return


def choose_install_mode_full_screen(color: bool) -> str:
    selected = 0
    last_size: os.terminal_size | None = None
    with alternate_terminal_screen(color=color) as file_descriptor:
        while True:
            size = shutil.get_terminal_size(fallback=(80, 30))
            if size != last_size:
                sys.stdout.write(full_screen_mode_frame(selected, color))
                sys.stdout.flush()
                last_size = size
            ready, _, _ = select.select([file_descriptor], [], [], 0.15)
            if not ready:
                continue
            key = read_terminal_key(file_descriptor)
            if key == "up":
                selected = (selected - 1) % 4
                last_size = None
            elif key == "down":
                selected = (selected + 1) % 4
                last_size = None
            elif key == "first":
                return "very-best-mods"
            elif key == "second":
                return "installed"
            elif key == "third":
                return "excogitare-patch"
            elif key == "fourth":
                return "restore-stock"
            elif key == "select":
                return (
                    "very-best-mods",
                    "installed",
                    "excogitare-patch",
                    "restore-stock",
                )[selected]
            elif key == "quit":
                return "quit"


@dataclass(frozen=True)
class Civ5Install:
    game_app: Path
    user_data: Path
    game_assets: Path
    dlc_dir: Path
    mods_dir: Path
    maps_dir: Path
    base_db: Path | None


@dataclass(frozen=True)
class InstalledMod:
    path: Path
    manifest: packer.Manifest
    map_info: packer.Civ5MapInfo | None = None


@dataclass(frozen=True)
class PreparedPackage:
    mod: InstalledMod
    path: Path
    install_name: str
    report: packer.BuildReport


def choose_install_mode(
    very_best_mods: bool,
    selection: str | None,
    maps: Sequence[Path],
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    color: bool = False,
    excogitare_patch: bool = False,
    restore_stock: bool = False,
) -> str:
    if restore_stock:
        if very_best_mods or excogitare_patch or selection is not None or maps:
            raise InstallerError(
                "--restore-stock is mutually exclusive with installation and --map"
            )
        return "restore-stock"
    if excogitare_patch:
        if very_best_mods or selection is not None or maps:
            raise InstallerError(
                "--excogitare-patch is mutually exclusive with mod selection and --map"
            )
        return "excogitare-patch"
    if very_best_mods:
        if selection is not None or maps:
            raise InstallerError(
                f"{best_mods.PRESET_NAME} is mutually exclusive with --select and --map"
            )
        return "very-best-mods"
    if selection is not None or maps:
        return "installed"
    if terminal_supports_full_screen(input_fn, output_fn):
        try:
            return choose_install_mode_full_screen(color)
        except KeyboardInterrupt:
            return "quit"
        except OSError:
            # A redirected or unusual terminal may claim TTY support but reject
            # termios. Fall back to the line-oriented selector safely.
            pass
    output_fn("\n" + installation_mode_menu(color))
    while True:
        value = input_fn("\nPROGRAM [1; Enter=1; 2; 3; 4; q] > ").strip().lower()
        if value in {"q", "quit"}:
            return "quit"
        if value in {"", "1"}:
            return "very-best-mods"
        if value == "2":
            return "installed"
        if value == "3":
            return "excogitare-patch"
        if value == "4":
            return "restore-stock"
        output_fn("  Choose 1, 2, 3, 4, or q.")


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
        maps_dir=user_data / "Maps",
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


def discover_maps(paths: Sequence[Path]) -> tuple[list[InstalledMod], list[str]]:
    maps: list[InstalledMod] = []
    skipped: list[str] = []
    candidates: list[Path] = []
    for value in paths:
        path = value.expanduser()
        if path.is_dir():
            candidates.extend(sorted(path.glob("*.Civ5Map"), key=lambda item: item.name.casefold()))
        else:
            candidates.append(path)
    seen: set[str] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            info = packer.inspect_civ5_map(resolved)
            maps.append(
                InstalledMod(
                    path=resolved,
                    manifest=packer.standalone_map_manifest(info),
                    map_info=info,
                )
            )
        except (packer.PackError, OSError, ValueError) as exc:
            skipped.append(f"{path.name}: {exc}")
    maps.sort(key=lambda item: (item.manifest.name.casefold(), item.path.name.casefold()))
    return maps, skipped


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
        raise InstallerError("no valid ModBuddy mods or Civ5Map files were found")
    output_fn("\nInstalled mods and maps:")
    for number, mod in enumerate(mods, 1):
        flags = []
        if mod.map_info is not None:
            flags.append(
                f"map {mod.map_info.width}x{mod.map_info.height}, {mod.map_info.world_size}"
            )
        elif mod.manifest.supports_multiplayer is False:
            flags.append("declares no multiplayer support")
        if mod.manifest.supports_mac is False:
            flags.append("declares no Mac support")
        suffix = f"  [{'; '.join(flags)}]" if flags else ""
        kind = "map" if mod.map_info is not None else f"v {mod.manifest.version}"
        output_fn(f"  {number:>2}. {mod.manifest.name} ({kind}){suffix}")
    prompt = "\nSelect mods/maps (for example 1,3-5 or all; q to quit): "
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
    source_name = mod.manifest.name.translate(
        str.maketrans({"—": "-", "–": "-", "−": "-", "’": "'", "“": '"', "”": '"'})
    )
    # Civ V's legacy DLC scanner accepts UTF-8 inside manifests but silently
    # skips packages reached through non-ASCII directory names.
    source_name = unicodedata.normalize("NFKD", source_name).encode("ascii", "ignore").decode("ascii")
    safe_name = re.sub(r"[/:\\\x00-\x1f]", "-", source_name).strip(" .")
    safe_name = re.sub(r"\s+", " ", safe_name) or "Unnamed Mod"
    safe_name = safe_name[:140].rstrip()
    return f"Civ5MP - {safe_name} (v {mod.manifest.version}) [{mod.manifest.mod_id[:8]}]"


def map_requires_engine_patch(mod: InstalledMod) -> bool:
    if mod.map_info is None:
        return False
    return (
        mod.map_info.world_size in EXCOGITARE_CUSTOM_WORLD_TYPES
        or max(mod.map_info.width, mod.map_info.height) > 255
        or mod.map_info.aspect_ratio >= 8
    )


def source_mod_conflict_warning() -> str:
    return (
        "Packed DLC loads automatically from standard Multiplayer or Single Player. "
        "Do not enable its original copy through the Mods menu at the same time; "
        "that loads the same gameplay twice and causes database and Lua errors."
    )


def entrypoint_script_name(value: str) -> str:
    target = value.split("->", 1)[-1]
    if ":" in target:
        target = target.split(":", 1)[1]
    name = Path(target).name
    return name.removesuffix(".lua")


def installed_generated_packages(dlc_dir: Path) -> list[tuple[Path, dict[str, object]]]:
    result: list[tuple[Path, dict[str, object]]] = []
    for report_path in sorted(dlc_dir.glob("*/pack-report.json")):
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("source_id"):
            result.append((report_path.parent, data))
    return result


@dataclass(frozen=True)
class StockRevertPlan:
    packages: tuple[Path, ...]
    cache_files: tuple[Path, ...]
    engine_state: str
    engine_backup: Path | None


def verified_wir_schaffen_package(path: Path) -> dict[str, object]:
    if not path.is_dir() or path.is_symlink():
        raise InstallerError(f"refusing unexpected generated DLC path: {path}")
    report_path = path / "pack-report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallerError(f"cannot verify generated DLC ownership: {path.name}") from exc
    if not isinstance(report, dict) or not report.get("source_id") or not report.get("dlc_id"):
        raise InstallerError(f"generated DLC report is incomplete: {path.name}")
    package_name = str(report.get("package", ""))
    if not package_name:
        candidates = list(path.glob("*.Civ5Pkg"))
        if len(candidates) != 1:
            raise InstallerError(f"generated DLC package manifest is ambiguous: {path.name}")
        package_name = candidates[0].name
    if Path(package_name).name != package_name:
        raise InstallerError(f"generated DLC report contains an unsafe package path: {path.name}")
    package_path = path / package_name
    try:
        package = ET.parse(package_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise InstallerError(f"generated DLC manifest cannot be verified: {path.name}") from exc
    if packer.local_name(package.tag) != "Civ5Package":
        raise InstallerError(f"generated DLC manifest has an invalid root: {path.name}")
    report_dlc_id = str(report["dlc_id"]).strip("{}").lower()
    package_dlc_id = (package.findtext("GUID") or "").strip().strip("{}").lower()
    supplied_key = (package.findtext("Key") or "").strip().lower()
    if package_dlc_id != report_dlc_id or supplied_key != packer.package_key_from_xml(package):
        raise InstallerError(f"generated DLC authentication failed: {path.name}")
    return report


def stock_revert_plan(install: Civ5Install) -> StockRevertPlan:
    package_paths: set[Path] = set()
    for candidate in install.dlc_dir.iterdir():
        if not candidate.name.startswith("Civ5MP -"):
            continue
        verified_wir_schaffen_package(candidate)
        package_paths.add(candidate)

    executable = install.game_app / ENGINE_RELATIVE_PATH
    state = engine_patch_state(executable)
    backup: Path | None = None
    if state == "original":
        if file_sha256(executable) != ENGINE_ORIGINAL_SHA256:
            raise InstallerError(
                "the executable has the original patch-site bytes but does not match the verified stock build"
            )
    elif state in {
        "divisor_guard",
        "zero_span_guard",
        "bounded_fallback_guard",
        "patched",
    }:
        backup = engine_backup_path(install.user_data)
        if not backup.is_file() or backup.is_symlink() or file_sha256(backup) != ENGINE_ORIGINAL_SHA256:
            raise InstallerError("a verified original executable backup is required for stock restoration")
    else:
        raise InstallerError(
            "the Civilization V executable is not a supported stock or Wir Schaffen DLC build; "
            "use Steam's Verify Integrity of Game Files"
        )

    cache_files: list[Path] = []
    cache_dir = install.user_data / "cache"
    for name in STOCK_REBUILD_CACHE_NAMES:
        path = cache_dir / name
        if path.exists():
            if not path.is_file() or path.is_symlink():
                raise InstallerError(f"refusing unexpected Civ V cache path: {path}")
            cache_files.append(path)
    return StockRevertPlan(
        packages=tuple(sorted(package_paths, key=lambda path: path.name.casefold())),
        cache_files=tuple(cache_files),
        engine_state=state,
        engine_backup=backup,
    )


def restore_stock_installation(
    install: Civ5Install,
    args: argparse.Namespace,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    terminal_ui: FullScreenTerminalUI | None = None,
) -> int:
    plan = stock_revert_plan(install)
    output_fn(f"\nSelected maintenance program: {RESTORE_STOCK_NAME}")
    output_fn(f"  • Remove {len(plan.packages)} authenticated Wir Schaffen DLC package(s)")
    output_fn(f"  • Engine state: {plan.engine_state}")
    output_fn(f"  • Invalidate {len(plan.cache_files)} generated database cache file(s)")
    output_fn("  • Preserve Firaxis DLC, saves, MODS, Maps, preferences, and screenshots")
    if args.dry_run:
        output_fn("\nDry run complete: stock restoration preflight passed. Nothing was changed.")
        return 0
    if not args.yes and not yes(input_fn("\nRestore the stock Civilization V installation? [y/N]: ")):
        output_fn("No changes made.")
        return 0

    staging = install.user_data / f".wir-schaffen-stock-revert-{uuid.uuid4().hex}"
    staging.mkdir()
    moved: list[tuple[Path, Path]] = []
    engine_was_restored = False
    try:
        package_total = max(1, len(plan.packages))
        if not plan.packages and terminal_ui is not None:
            terminal_ui.set_progress(
                "REMOVE", "No generated DLC packages installed", 1, 1, 1, 1,
                detail="Firaxis DLC left untouched",
            )
        for position, package in enumerate(plan.packages, 1):
            if terminal_ui is not None:
                terminal_ui.set_progress(
                    "REMOVE", package.name, position, package_total, 0, 1,
                    detail="Moving authenticated generated DLC out of the game",
                )
            target = staging / f"dlc-{position:03d}-{package.name}"
            package.rename(target)
            moved.append((package, target))
            if terminal_ui is not None:
                terminal_ui.set_progress(
                    "REMOVE", package.name, position, package_total, 1, 1,
                    detail="Generated DLC removed",
                )

        if terminal_ui is not None:
            terminal_ui.set_progress(
                "RESTORE", "Civilization V executable", 1, 1, 0, 1,
                detail="Verifying and restoring the stock executable",
            )
        if plan.engine_state != "original":
            restore_colossal_engine_patch(install.game_app, install.user_data)
            engine_was_restored = True
        executable = install.game_app / ENGINE_RELATIVE_PATH
        if engine_patch_state(executable) != "original" or file_sha256(executable) != ENGINE_ORIGINAL_SHA256:
            raise InstallerError("the restored Civilization V executable failed stock verification")
        if terminal_ui is not None:
            terminal_ui.set_progress(
                "RESTORE", "Civilization V executable", 1, 1, 1, 1,
                detail="Verified stock executable restored",
            )

        cache_total = max(1, len(plan.cache_files))
        if not plan.cache_files and terminal_ui is not None:
            terminal_ui.set_progress(
                "CACHE", "No generated database cache present", 1, 1, 1, 1,
                detail="Civ V will use a clean database on next launch",
            )
        for position, cache_file in enumerate(plan.cache_files, 1):
            if terminal_ui is not None:
                terminal_ui.set_progress(
                    "CACHE", cache_file.name, position, cache_total, 0, 1,
                    detail="Removing generated cache; Civ V will rebuild it",
                )
            target = staging / f"cache-{position:03d}-{cache_file.name}"
            cache_file.rename(target)
            moved.append((cache_file, target))
            if terminal_ui is not None:
                terminal_ui.set_progress(
                    "CACHE", cache_file.name, position, cache_total, 1, 1,
                    detail="Cache invalidated",
                )

        backup = engine_backup_path(install.user_data)
        if backup.exists():
            if not backup.is_file() or backup.is_symlink():
                raise InstallerError(f"refusing unexpected engine backup path: {backup}")
            backup_target = staging / backup.name
            backup.rename(backup_target)
            moved.append((backup, backup_target))
        shutil.rmtree(staging)
        backup_dir = install.user_data / ENGINE_BACKUP_DIR
        if backup_dir.is_dir() and not any(backup_dir.iterdir()):
            backup_dir.rmdir()
    except Exception:
        for original, target in reversed(moved):
            if target.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                target.rename(original)
        if engine_was_restored and engine_patch_state(install.game_app / ENGINE_RELATIVE_PATH) == "original":
            apply_colossal_engine_patch(install.game_app, install.user_data)
        if staging.exists() and not any(staging.iterdir()):
            staging.rmdir()
        raise

    output_fn("\n✓ Stock Civilization V installation restored and verified.")
    output_fn("Official Firaxis DLC and all user saves, MODS, Maps, preferences, and screenshots were preserved.")
    return 0


def validate_exclusive_mode(dlc_dir: Path, mode: str) -> None:
    installed = installed_generated_packages(dlc_dir)
    if mode == "installed":
        locked = [
            path
            for path, report in installed
            if isinstance(report.get("collection"), dict)
            and report["collection"].get("id") == best_mods.PRESET_ID
        ]
        if locked:
            raise InstallerError(
                f"{best_mods.PRESET_NAME} is installed as an exclusive collection; "
                "remove its generated DLC folders before packaging other mods"
            )
        return
    if mode != "very-best-mods":
        return
    allowed_ids = {source.manifest_id for source in best_mods.SOURCES}
    allowed_ids.add(SHARED_UI_BRIDGE_ID)
    allowed_ids.add(EXCOGITARE_PATCH_ID)
    conflicts: list[Path] = []
    for path, report in installed:
        collection = report.get("collection")
        if isinstance(collection, dict) and collection.get("id") == best_mods.PRESET_ID:
            continue
        if str(report.get("source_id")) in allowed_ids:
            continue
        conflicts.append(path)
    if conflicts:
        raise InstallerError(
            f"{best_mods.PRESET_NAME} cannot coexist with other Wir Schaffen DLC packages: "
            + ", ".join(path.name for path in conflicts)
        )


def stamp_collection(
    packages: Sequence[PreparedPackage], metadata: dict[str, object]
) -> None:
    for package in packages:
        report_path = package.path / "pack-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["collection"] = metadata
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def effective_generated_packages(
    prepared: Sequence[PreparedPackage], dlc_dir: Path
) -> list[tuple[Path, dict[str, object]]]:
    selected_ids = {package.report.source_id for package in prepared}
    effective: dict[str, tuple[Path, dict[str, object]]] = {}
    for report_path in dlc_dir.glob("*/pack-report.json"):
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            source_id = str(data["source_id"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if data.get("source_kind") == "SharedUIBridge" or source_id in selected_ids:
            continue
        effective[source_id] = (report_path.parent, data)
    for package in prepared:
        data = json.loads((package.path / "pack-report.json").read_text(encoding="utf-8"))
        effective[package.report.source_id] = (package.path, data)
    return sorted(
        effective.values(),
        key=lambda item: (str(item[1].get("source_mod", "")).casefold(), str(item[1].get("source_id", ""))),
    )


def validate_combined_namespace(packages: Sequence[tuple[Path, dict[str, object]]]) -> dict[str, Path]:
    namespace: dict[str, tuple[Path, str, bytes]] = {}
    entry_files: dict[str, Path] = {}
    for package_path, report in packages:
        source_name = str(report.get("source_mod", package_path.name))
        for database_name in (*report.get("game_data", []), *report.get("text_data", [])):
            path = package_path / str(database_name)
            if not path.is_file():
                raise InstallerError(
                    f"combined package is missing database file {database_name}: {source_name}"
                )
            key = path.name.casefold()
            data = path.read_bytes()
            previous = namespace.get(key)
            if previous is not None and previous[2] != data:
                raise InstallerError(
                    f"combined virtual-file collision for {path.name}: "
                    f"{previous[1]} and {source_name} contain different files"
                )
            namespace.setdefault(key, (path, source_name, data))
        for directory_name in ("Files", "UI", "Maps"):
            directory = package_path / directory_name
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not path.is_file():
                    continue
                if directory_name == "UI" and path.name.casefold() == "ingame.lua":
                    continue
                key = path.name.casefold()
                data = path.read_bytes()
                previous = namespace.get(key)
                if previous is not None and previous[2] != data:
                    raise InstallerError(
                        f"combined virtual-file collision for {path.name}: "
                        f"{previous[1]} and {source_name} contain different files"
                    )
                namespace.setdefault(key, (path, source_name, data))
                if path.suffix.casefold() == ".lua":
                    entry_files[key] = path
    return entry_files


def build_excogitare_patch_package(
    install: Civ5Install,
    staging_dir: Path,
    output_fn: Callable[[str], None] = print,
) -> PreparedPackage:
    output_fn("\n→ Building Excogitare map-size compatibility DLC...")
    manifest = packer.Manifest(
        path=staging_dir / "generated.modinfo",
        mod_id=EXCOGITARE_PATCH_ID,
        version=1,
        name=EXCOGITARE_PATCH_NAME,
        description=(
            "Registers Excogitare's Extreme and Colossal sizes; the companion "
            "engine guard supports its extreme rectangular geometries."
        ),
        supports_multiplayer=True,
        supports_mac=True,
        dependencies=(),
        references=(),
        files=(),
        actions=(),
        entry_points=(),
    )
    mod = InstalledMod(path=staging_dir, manifest=manifest)
    output = staging_dir / package_install_name(mod)
    output.mkdir()

    game_root = ET.Element("GameData")
    game_worlds = ET.SubElement(game_root, "Worlds")
    text_root = ET.Element("GameData")
    text_language = ET.SubElement(text_root, "Language_en_US")
    for world_size, width, height, players, city_states in EXCOGITARE_WORLD_SIZES:
        world_xml = packer.world_compatibility_xml(
            world_size,
            width,
            height,
            players,
            install.base_db,
            default_minor_civs=city_states,
        )
        world_row = world_xml.find("./Worlds/Replace")
        if world_row is None:
            raise InstallerError(f"failed to build {world_size} compatibility metadata")
        game_worlds.append(world_row)
        language_xml = packer.world_compatibility_text_xml(world_size, width, height)
        for language_row in language_xml.findall("./Language_en_US/Replace"):
            text_language.append(language_row)

    game_name = "001_Excogitare_CustomWorlds.xml"
    text_name = "002_Excogitare_CustomWorlds_Text.xml"
    packer.write_xml(output / game_name, game_root)
    packer.write_xml(output / text_name, text_root)
    dlc_id = uuid.uuid5(packer.PACKER_NAMESPACE, "wir-schaffen-dlc:excogitare-map-patch:dlc")
    package_name = packer.write_package(
        output,
        manifest,
        dlc_id,
        5,
        "Expansion2",
        (game_name,),
        (text_name,),
        False,
        False,
        False,
    )
    validation = packer.validate_output(
        output,
        package_name,
        False,
        False,
        base_db=install.base_db,
    )
    validation.extend(
        [
            "Excogitare Extreme is registered as 180x94 with 20 major and 20 minor slots",
            "Excogitare Colossal is registered as 170x110 with 22 major and 22 minor slots",
            "Tall, Wide, and Square maps retain their native Civ5Map dimensions",
            "Needle, Ribbon, Pin, and String maps are paired with the reversible engine geometry guard",
        ]
    )
    report = packer.BuildReport(
        source_mod=manifest.name,
        source_id=manifest.mod_id,
        source_version=manifest.version,
        dlc_id=str(dlc_id),
        ui_set="Expansion2",
        game_data=[game_name],
        text_data=[text_name],
        imported_files=[],
        maps=[],
        entry_points=[],
        translated_sql=[],
        omitted_features=[],
        warnings=[
            "Excogitare's game-breaking sizes and geometries can still exceed Civ V's "
            "memory, minimap, pathfinding, or late-game practical limits."
        ],
        validation=validation,
    )
    report_data = dict(report.__dict__)
    report_data.update(
        {
            "package": package_name,
            "source_kind": "ExcogitareMapPatch",
            "world_sizes": [
                {
                    "type": world_size,
                    "width": width,
                    "height": height,
                    "default_players": players,
                    "default_minor_civs": city_states,
                }
                for world_size, width, height, players, city_states in EXCOGITARE_WORLD_SIZES
            ],
            "geometries": [
                "STANDARD",
                "TALL",
                "WIDE",
                "SQUARE",
                "NEEDLE",
                "RIBBON",
                "PIN",
                "STRING",
            ],
            "output_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file()),
        }
    )
    (output / "pack-report.json").write_text(
        json.dumps(report_data, indent=2) + "\n",
        encoding="utf-8",
    )
    output_fn("  ✓ Extreme, Colossal, and custom-geometry metadata ready.")
    return PreparedPackage(
        mod=InstalledMod(path=output, manifest=manifest),
        path=output,
        install_name=package_install_name(mod),
        report=report,
    )


def build_shared_ui_bridge(
    prepared: Sequence[PreparedPackage],
    install: Civ5Install,
    staging_dir: Path,
    output_fn: Callable[[str], None] = print,
) -> PreparedPackage | None:
    packages = effective_generated_packages(prepared, install.dlc_dir)
    available_lua = validate_combined_namespace(packages)
    scripts: list[str] = []
    members: list[dict[str, object]] = []
    seen_scripts: set[str] = set()
    for _, report in packages:
        members.append(
            {
                "source_mod": report.get("source_mod"),
                "source_id": report.get("source_id"),
                "source_version": report.get("source_version"),
            }
        )
        for entry in report.get("entry_points", []):
            script = entrypoint_script_name(str(entry))
            key = f"{script}.lua".casefold()
            if key not in available_lua:
                raise InstallerError(
                    f"shared UI bridge cannot find the packaged entry-point script {script}.lua"
                )
            if key not in seen_scripts:
                seen_scripts.add(key)
                scripts.append(script)
    if not scripts:
        return None

    output_fn(f"\n→ Coordinating {len(scripts)} isolated in-game UI entry points...")
    output = staging_dir / "Civ5MP - Wir Schaffen DLC Shared UI Bridge"
    output.mkdir()
    ui = output / "UI"
    ui.mkdir()
    source = packer.effective_ui_source(install.game_assets, "Expansion2", "InGame.lua")
    text = source.read_text(encoding="utf-8-sig").rstrip()
    injected = [
        "",
        "-- BEGIN WIR SCHAFFEN DLC SHARED UI BRIDGE",
        "g_WirSchaffenDlcAddins = g_WirSchaffenDlcAddins or {};",
    ]
    for script in scripts:
        injected.append(
            "table.insert(g_WirSchaffenDlcAddins, "
            f"ContextPtr:LoadNewContext({json.dumps(script)}));"
        )
    injected.append("-- END WIR SCHAFFEN DLC SHARED UI BRIDGE")
    (ui / "InGame.lua").write_text(text + "\n" + "\n".join(injected) + "\n", encoding="utf-8")

    manifest = packer.Manifest(
        path=output / "generated.modinfo",
        mod_id=SHARED_UI_BRIDGE_ID,
        version=1,
        name="Wir Schaffen DLC Shared UI Bridge",
        description="Coordinates isolated InGameUIAddin contexts for the installed packed mod set.",
        supports_multiplayer=True,
        supports_mac=True,
        dependencies=(),
        references=(),
        files=(),
        actions=(),
        entry_points=(),
    )
    dlc_id = uuid.uuid5(packer.PACKER_NAMESPACE, "wir-schaffen-dlc:shared-ui-bridge:dlc")
    package_name = packer.write_package(
        output,
        manifest,
        dlc_id,
        1,
        "Expansion2",
        (),
        (),
        False,
        True,
        False,
    )
    validation = packer.validate_output(output, package_name, False, False)
    generated = (ui / "InGame.lua").read_text(encoding="utf-8")
    for script in scripts:
        if f'ContextPtr:LoadNewContext("{script}")' not in generated:
            raise InstallerError(f"shared UI bridge omitted {script}")
    validation.extend(
        [
            f"combined virtual namespace is collision-free across {len(packages)} packages",
            f"{len(scripts)} InGameUIAddin scripts load in isolated Lua contexts",
        ]
    )
    report = packer.BuildReport(
        source_mod=manifest.name,
        source_id=manifest.mod_id,
        source_version=manifest.version,
        dlc_id=str(dlc_id),
        ui_set="Expansion2",
        game_data=[],
        text_data=[],
        imported_files=[],
        maps=[],
        entry_points=[f"InGameUIAddin:{script}.lua" for script in scripts],
        translated_sql=[],
        omitted_features=[],
        warnings=[],
        validation=validation,
    )
    report_data = dict(report.__dict__)
    report_data.update(
        {
            "package": package_name,
            "source_kind": "SharedUIBridge",
            "members": members,
            "entrypoint_scripts": scripts,
            "output_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file()),
        }
    )
    (output / "pack-report.json").write_text(json.dumps(report_data, indent=2) + "\n", encoding="utf-8")
    mod = InstalledMod(path=output, manifest=manifest)
    output_fn("  ✓ Shared bridge ready; per-mod UI hooks will not compete by load order.")
    return PreparedPackage(
        mod=mod,
        path=output,
        install_name=package_install_name(mod),
        report=report,
    )


def prepare_packages(
    mods: Sequence[InstalledMod],
    install: Civ5Install,
    staging_dir: Path,
    output_fn: Callable[[str], None] = print,
    progress_fn: Callable[[int, int, str, bool], None] | None = None,
) -> list[PreparedPackage]:
    prepared: list[PreparedPackage] = []
    for position, mod in enumerate(mods, 1):
        if progress_fn is not None:
            progress_fn(position, len(mods), mod.manifest.name, False)
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
            map_file=mod.map_info is not None,
            defer_entrypoints=True,
        )
        report = packer.pack(args)
        for warning in report.warnings:
            output_fn(f"  ! {warning}")
        prepared.append(PreparedPackage(mod=mod, path=output_path, install_name=install_name, report=report))
        output_fn("  ✓ Ready.")
        if progress_fn is not None:
            progress_fn(position, len(mods), mod.manifest.name, True)
    bridge = build_shared_ui_bridge(prepared, install, staging_dir, output_fn)
    if bridge is not None:
        prepared.append(bridge)
    return prepared


def _remove_directory(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise InstallerError(f"refusing to remove unexpected path: {path}")
        shutil.rmtree(path)


def matching_installed_packages(package: PreparedPackage, dlc_dir: Path) -> list[Path]:
    destination = dlc_dir / package.install_name
    matches: set[Path] = {destination} if destination.exists() else set()
    identity = (package.mod.manifest.mod_id, package.mod.manifest.version)
    for candidate in dlc_dir.iterdir():
        if candidate == destination or not candidate.is_dir() or candidate.is_symlink():
            continue
        report_path = candidate / "pack-report.json"
        if not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            candidate_identity = (str(report["source_id"]), int(report["source_version"]))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if candidate_identity == identity:
            matches.add(candidate)
    return sorted(matches, key=lambda path: path.name.casefold())


def install_packages(
    packages: Sequence[PreparedPackage],
    dlc_dir: Path,
    replace: bool,
    progress_fn: Callable[[int, int, str, bool], None] | None = None,
) -> list[Path]:
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
    existing_by_package = [matching_installed_packages(package, dlc_dir) for package in packages]
    existing = [path for matches in existing_by_package for path in matches]
    if existing and not replace:
        raise InstallerError("already installed (use --replace to update): " + ", ".join(path.name for path in existing))
    unsafe_existing = [path for path in existing if not path.is_dir() or path.is_symlink()]
    if unsafe_existing:
        raise InstallerError("refusing to replace unexpected DLC path: " + ", ".join(map(str, unsafe_existing)))

    transaction = uuid.uuid4().hex
    changes: list[tuple[Path, list[tuple[Path, Path]]]] = []
    incoming_paths: list[Path] = []
    try:
        for index, (package, destination, package_existing) in enumerate(
            zip(packages, destinations, existing_by_package)
        ):
            if progress_fn is not None:
                progress_fn(index + 1, len(packages), package.mod.manifest.name, False)
            incoming = dlc_dir / f".civ5-mod-dlc-incoming-{transaction}-{index}"
            incoming_paths.append(incoming)
            shutil.copytree(package.path, incoming)
            backups: list[tuple[Path, Path]] = []
            try:
                for old_index, old_path in enumerate(package_existing):
                    backup = dlc_dir / f".civ5-mod-dlc-backup-{transaction}-{index}-{old_index}"
                    old_path.rename(backup)
                    backups.append((old_path, backup))
                incoming.rename(destination)
            except Exception:
                for old_path, backup in reversed(backups):
                    if backup.exists():
                        backup.rename(old_path)
                raise
            changes.append((destination, backups))
            if progress_fn is not None:
                progress_fn(index + 1, len(packages), package.mod.manifest.name, True)
    except Exception:
        for destination, backups in reversed(changes):
            _remove_directory(destination)
            for old_path, backup in reversed(backups):
                if backup.exists():
                    backup.rename(old_path)
        for incoming in incoming_paths:
            _remove_directory(incoming)
        raise
    for _, backups in changes:
        for _, backup in backups:
            _remove_directory(backup)
    return destinations


def yes(value: str) -> bool:
    return value.strip().lower() in {"y", "yes"}


def install_excogitare_patch(
    install: Civ5Install,
    args: argparse.Namespace,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    terminal_ui: FullScreenTerminalUI | None = None,
) -> int:
    if args.no_engine_patch:
        raise InstallerError(
            "--no-engine-patch cannot be combined with --excogitare-patch; "
            "the geometry guard is part of this patch"
        )
    executable = install.game_app / ENGINE_RELATIVE_PATH
    state = engine_patch_state(executable)
    if state == "unsupported":
        raise InstallerError(
            "the installed executable does not match the supported Excogitare geometry patch sites"
        )

    output_fn(f"\nSelected compatibility program: {EXCOGITARE_PATCH_NAME}")
    output_fn("  • Registers WORLDSIZE_EXTREME at 180x94")
    output_fn("  • Registers WORLDSIZE_COLOSSAL at 170x110")
    output_fn("  • Applies the reversible custom-geometry engine guard")
    output_fn(
        "  ! Needle, Ribbon, Pin, String, Extreme, and Colossal remain experimental; "
        "this patch addresses known loader/projection failures, not every resource limit."
    )
    output_fn("\nQuit Civilization V before continuing.")

    with tempfile.TemporaryDirectory(prefix="civ5-excogitare-patch-") as temp:
        if terminal_ui is not None:
            terminal_ui.set_progress(
                "PACKAGE",
                EXCOGITARE_PATCH_NAME,
                1,
                1,
                0,
                1,
                detail="Building authenticated world-size DLC",
            )
        package = build_excogitare_patch_package(install, Path(temp), output_fn)
        if terminal_ui is not None:
            terminal_ui.set_progress(
                "PACKAGE",
                EXCOGITARE_PATCH_NAME,
                1,
                1,
                1,
                1,
                detail="Compatibility DLC validated",
            )
        for warning in package.report.warnings:
            output_fn(f"  ! {warning}")
        if args.dry_run:
            output_fn(
                f"\nDry run complete: compatibility DLC validated; engine guard state is {state}. "
                "Nothing was installed."
            )
            return 0

        existing = matching_installed_packages(package, install.dlc_dir)
        replace = args.replace
        if existing and not replace:
            output_fn("\nThe Excogitare compatibility package is already installed:")
            for path in existing:
                output_fn(f"  {path.name}")
            if args.yes or not yes(input_fn("Replace it? [y/N]: ")):
                if args.yes:
                    raise InstallerError(
                        "the Excogitare package already exists; pass --replace to update it"
                    )
                output_fn("No changes made.")
                return 0
            replace = True

        if not args.yes and not yes(
            input_fn("\nInstall the compatibility DLC and patch Civilization V? [y/N]: ")
        ):
            output_fn("No changes made.")
            return 0

        if terminal_ui is not None:
            terminal_ui.set_progress(
                "INSTALL",
                "Reversible native geometry guard",
                1,
                2,
                0,
                1,
                detail="Patching and ad-hoc signing Civilization V",
            )
        newly_patched = apply_colossal_engine_patch(install.game_app, install.user_data)
        if terminal_ui is not None:
            terminal_ui.set_progress(
                "INSTALL",
                "Reversible native geometry guard",
                1,
                2,
                1,
                1,
                detail="Engine guard verified",
            )
        output_fn(
            "\n✓ Applied and ad-hoc signed the reversible Excogitare geometry guard."
            if newly_patched
            else "\n✓ The Excogitare geometry guard is already applied."
        )
        try:
            install_progress = None
            if terminal_ui is not None:
                install_progress = lambda position, total, title, complete: terminal_ui.install_progress(
                    position + 1,
                    total + 1,
                    title,
                    complete,
                )
            destinations = install_packages(
                [package],
                install.dlc_dir,
                replace=replace,
                progress_fn=install_progress,
            )
        except Exception:
            if newly_patched:
                restore_colossal_engine_patch(install.game_app, install.user_data)
            raise

    output_fn("\n✓ Excogitare map compatibility installed successfully:")
    for destination in destinations:
        output_fn(f"  {destination.name}")
    output_fn(
        "\nEvery macOS multiplayer participant using an Excogitare custom size or "
        "extreme geometry needs the same compatibility DLC and engine guard."
    )
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {packer.VERSION}")
    parser.add_argument("--game-app", type=Path, help="path to Civilization V.app")
    parser.add_argument("--user-data", type=Path, help="path containing the MODS and cache directories")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--gamecore', choices=('status', 'stock', 'lekmod', 'vox-populi', 'recover'),
                      help='inspect, restore, install/update/switch, or recover native GameCore products')
    parser.add_argument('--gamecore-package', type=Path, help='local native release ZIP (requires its independently recorded SHA-256)')
    parser.add_argument('--gamecore-sha256', help='trusted SHA-256 of the local native release ZIP')
    mode.add_argument("--select", help="non-interactive installed-mod selection, such as 1,3-5 or all")
    mode.add_argument(
        "--very-best-mods",
        action="store_true",
        help=f"download and package the exclusive {best_mods.PRESET_NAME} collection",
    )
    mode.add_argument(
        "--excogitare-patch",
        action="store_true",
        help="install AngelaDMerkel's Excogitare custom-size and geometry compatibility patch",
    )
    mode.add_argument(
        "--restore-stock",
        action="store_true",
        help="remove all verified Wir Schaffen DLC changes and restore stock Civilization V",
    )
    parser.add_argument(
        "--map",
        type=Path,
        action="append",
        default=[],
        help="also offer this standalone .Civ5Map for selection (repeatable)",
    )
    parser.add_argument("--yes", action="store_true", help="install without the final confirmation")
    parser.add_argument("--replace", action="store_true", help="replace matching packages installed by this tool")
    parser.add_argument(
        "--no-engine-patch",
        action="store_true",
        help="do not apply the reversible custom-size and geometry engine guard while packaging maps",
    )
    parser.add_argument(
        "--restore-engine-patch",
        action="store_true",
        help="restore the original executable from the verified Excogitare-guard backup and exit",
    )
    parser.add_argument("--dry-run", action="store_true", help="package and validate, but do not install")
    return parser


def run_gamecore(
    args: argparse.Namespace,
    install: Civ5Install,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> int:
    manager = gamecore.ProductManager(install.game_app, install.user_data)
    product = args.gamecore
    package_path = getattr(args, 'gamecore_package', None)
    digest = getattr(args, 'gamecore_sha256', None)
    if args.map or args.restore_engine_patch:
        raise InstallerError('native GameCore actions cannot be combined with map or executable-patch actions')
    if bool(package_path) != bool(digest):
        raise InstallerError('--gamecore-package and --gamecore-sha256 must be supplied together')
    if product in ('status', 'recover', 'stock') and package_path:
        raise InstallerError('status, recovery, and stock restoration do not accept a product archive')
    if product == 'status':
        output_fn(json.dumps(manager.status(), indent=2))
        return 0
    if product == 'recover':
        output_fn(json.dumps(manager.status(), indent=2))
        if args.dry_run:
            return 0
        if not args.yes and not yes(input_fn('Restore the installation from the interrupted transaction? [y/N]: ')):
            return 0
        if subprocess.run(['pgrep', '-x', 'Civilization V'], capture_output=True).returncode == 0:
            raise InstallerError('Quit Civilization V before recovering a GameCore transaction')
        manager.recover()
        output_fn(json.dumps(manager.status(), indent=2))
        return 0
    with tempfile.TemporaryDirectory(prefix='wir-schaffen-native-') as temporary:
        with contextlib.ExitStack() as stack:
            package = None
            manifest = None
            if product != 'stock':
                if package_path is None:
                    package_path, digest = gamecore.download_release(product, Path(temporary) / 'release.zip')
                package, manifest = stack.enter_context(gamecore.open_artifact(package_path, digest))
            plan = manager.plan(product, manifest)
            output_fn(json.dumps(plan, indent=2))
            if args.dry_run:
                output_fn('Dry run complete. No application or installation state was changed.')
                return 0
            if not args.yes and not yes(input_fn(f'Activate {product} and its matching content? [y/N]: ')):
                output_fn('No changes made.')
                return 0
            if subprocess.run(['pgrep', '-x', 'Civilization V'], capture_output=True).returncode == 0:
                raise InstallerError('Quit Civilization V before switching GameCore products')
            output_fn(json.dumps(manager.switch(product, package), indent=2))
    return 0


def run(
    args: argparse.Namespace,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    terminal_ui: FullScreenTerminalUI | None = None,
) -> int:
    use_color = terminal_supports_color() and output_fn is print
    if terminal_ui is None:
        output_fn(terminal_banner(color=use_color))

    game_apps = [args.game_app] if args.game_app else discover_game_apps()
    user_dirs = [args.user_data] if args.user_data else discover_user_data_dirs()
    game_app = choose_path("Civilization V application", game_apps, input_fn, output_fn)
    user_data = choose_path("Civilization V user-data directory", user_dirs, input_fn, output_fn)
    install = validate_install(game_app, user_data)
    if getattr(args, 'gamecore', None):
        return run_gamecore(args, install, input_fn, output_fn)
    if getattr(args, 'gamecore_package', None) or getattr(args, 'gamecore_sha256', None):
        raise InstallerError('native package options require --gamecore')
    # The existing content/engine modes must not claim stock restoration or
    # introduce a second product while a custom native GameCore is active.
    native_binary = install.game_app / gamecore.BINARY_RELATIVE
    if native_binary.exists():
        manager = gamecore.ProductManager(install.game_app, install.user_data)
        native_state = manager._state()
        # The existing executable-patch modes verify their own supported host
        # hashes. A stock GameCore must not block restoring a patched executable.
        has_native_payload = any(manager._payload_path(product).exists() for product in gamecore.PRODUCTS)
        if (gamecore.sha256(native_binary) not in gamecore.CATALOG['stock_hashes']
                or manager.transaction.exists() or has_native_payload
                or (native_state and native_state['product'] != 'stock')):
            raise InstallerError('Native GameCore requires attention; use --gamecore status, then --gamecore stock before other installation modes')
    if args.restore_engine_patch:
        restored = restore_colossal_engine_patch(install.game_app, install.user_data)
        output_fn(
            "\n✓ Restored and verified the original Civilization V executable."
            if restored
            else "\nThe original Civilization V executable is already in place."
        )
        return 0
    if (
        terminal_ui is not None
        and not args.very_best_mods
        and args.select is None
        and not args.map
        and not args.excogitare_patch
        and not args.restore_stock
    ):
        mode = terminal_ui.choose_install_mode()
    else:
        mode = choose_install_mode(
            args.very_best_mods,
            args.select,
            args.map,
            input_fn,
            output_fn,
            color=use_color,
            excogitare_patch=args.excogitare_patch,
            restore_stock=args.restore_stock,
        )
    if mode == "quit":
        output_fn("No changes made.")
        return 0
    if terminal_ui is not None and terminal_ui.program != PROGRAM_LABELS.get(mode):
        terminal_ui.set_program(mode)
    validate_exclusive_mode(install.dlc_dir, mode)

    output_fn(f"\nGame: {install.game_app}")
    output_fn(f"Mods: {install.mods_dir}")
    output_fn(f"DLC:  {install.dlc_dir}")
    if mode == "restore-stock":
        return restore_stock_installation(
            install,
            args,
            input_fn,
            output_fn,
            terminal_ui=terminal_ui,
        )
    if install.base_db is None:
        output_fn("! No Civ5CoreDatabase.db cache was found; mods needing SQL compatibility will fail.")

    if mode == "excogitare-patch":
        return install_excogitare_patch(
            install,
            args,
            input_fn,
            output_fn,
            terminal_ui=terminal_ui,
        )

    selected: list[InstalledMod] = []
    skipped: list[str] = []
    if mode == "installed":
        mods, skipped = discover_mods(install.mods_dir)
        maps, skipped_maps = discover_maps([install.maps_dir, *args.map])
        skipped.extend(skipped_maps)
        selected = choose_mods([*mods, *maps], args.select, input_fn, output_fn)
        if not selected:
            output_fn("No changes made.")
            return 0
        if skipped:
            output_fn(
                f"\nSkipped {len(skipped)} invalid mod/map item"
                f"{'s' if len(skipped) != 1 else ''}."
            )
    else:
        output_fn(f"\nSelected exclusive collection: {best_mods.PRESET_NAME}")
        for source in best_mods.SOURCES:
            output_fn(f"  • {source.title}")
        output_fn(
            "  ! The supplied Workable Mountains link targets Civilization VI; "
            "the preset uses the author's Civ V Workshop item 233614126."
        )

    output_fn("\nQuit Civilization V before continuing.")
    needs_engine_patch = False
    if mode == "installed":
        needs_engine_patch = any(map_requires_engine_patch(item) for item in selected)
    if needs_engine_patch and args.no_engine_patch:
        output_fn(
            "! The selected map uses an Excogitare custom size or extreme geometry, but the "
            "engine guard was disabled; the native macOS build can crash or hang while loading it."
        )
    with tempfile.TemporaryDirectory(prefix="civ5-mod-dlc-") as temp:
        temporary_root = Path(temp)
        try:
            downloaded: list[best_mods.DownloadedMod] = []
            staging_dir = temporary_root
            if mode == "very-best-mods":
                downloaded = best_mods.download_preset(
                    temporary_root / "authoritative-sources",
                    output_fn,
                    progress_fn=terminal_ui.download_progress if terminal_ui is not None else None,
                )
                selected = [
                    InstalledMod(path=item.path, manifest=packer.parse_manifest(item.path))
                    for item in downloaded
                ]
                staging_dir = temporary_root / "packages"
                staging_dir.mkdir()
            prepared = prepare_packages(
                selected,
                install,
                staging_dir,
                output_fn,
                progress_fn=terminal_ui.package_progress if terminal_ui is not None else None,
            )
            if mode == "very-best-mods":
                stamp_collection(prepared, best_mods.collection_metadata(downloaded))
        except (
            best_mods.DownloadError,
            packer.PackError,
            ET.ParseError,
            OSError,
            ValueError,
        ):
            output_fn("\n✗ Packaging stopped; the Civilization V DLC folder was not changed.")
            raise
        if args.dry_run:
            bridge_count = sum(package.report.source_id == SHARED_UI_BRIDGE_ID for package in prepared)
            bridge_note = " plus the shared UI bridge" if bridge_count else ""
            output_fn(
                f"\nDry run complete: {len(selected)} selected item(s){bridge_note} packaged and validated; "
                "nothing installed."
            )
            if mode == "very-best-mods":
                output_fn("The exclusive collection and all source hashes were validated together.")
            if needs_engine_patch and not args.no_engine_patch:
                state = engine_patch_state(install.game_app / ENGINE_RELATIVE_PATH)
                if state == "unsupported":
                    raise InstallerError(
                        "the installed executable does not match the supported Excogitare geometry patch sites"
                    )
                output_fn(f"Engine guard check: {state}; it would be applied during installation if needed.")
            return 0

        existing = [
            path
            for package in prepared
            for path in matching_installed_packages(package, install.dlc_dir)
        ]
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
        newly_patched = False
        install_offset = 1 if needs_engine_patch and not args.no_engine_patch else 0
        if needs_engine_patch and not args.no_engine_patch:
            if terminal_ui is not None:
                terminal_ui.set_progress(
                    "INSTALL",
                    "Reversible native geometry guard",
                    1,
                    len(prepared) + 1,
                    0,
                    1,
                    detail="Patching and ad-hoc signing Civilization V",
                )
            newly_patched = apply_colossal_engine_patch(install.game_app, install.user_data)
            if terminal_ui is not None:
                terminal_ui.set_progress(
                    "INSTALL",
                    "Reversible native geometry guard",
                    1,
                    len(prepared) + 1,
                    1,
                    1,
                    detail="Engine guard verified",
                )
            output_fn(
                "\n✓ Applied and ad-hoc signed the reversible Excogitare geometry guard."
                if newly_patched
                else "\n✓ The Excogitare geometry guard is already applied."
            )
        try:
            install_progress = None
            if terminal_ui is not None:
                install_progress = lambda position, total, title, complete: terminal_ui.install_progress(
                    position + install_offset,
                    total + install_offset,
                    title,
                    complete,
                )
            destinations = install_packages(
                prepared,
                install.dlc_dir,
                replace=replace,
                progress_fn=install_progress,
            )
        except Exception:
            if newly_patched:
                restore_colossal_engine_patch(install.game_app, install.user_data)
            raise

    output_fn("\n✓ Installed successfully:")
    for destination in destinations:
        output_fn(f"  {destination.name}")
    if any(item.map_info is None for item in selected):
        output_fn("\n! " + source_mod_conflict_warning())
    output_fn("\nEvery multiplayer participant must install the same generated packages.")
    if mode == "very-best-mods":
        output_fn(
            f"{best_mods.PRESET_NAME} is exclusive; Wir Schaffen DLC will refuse other "
            "generated mod packages while any collection member remains installed."
        )
    if needs_engine_patch and not args.no_engine_patch:
        output_fn(
            "Mac participants using this custom size or geometry also need the same engine guard."
        )
    return 0


def run_full_screen(args: argparse.Namespace) -> int:
    with FullScreenTerminalUI(color=terminal_supports_color()) as ui:
        try:
            result = run(
                args,
                input_fn=ui.input,
                output_fn=ui.output,
                terminal_ui=ui,
            )
        except KeyboardInterrupt:
            ui.output("\nNo changes made.")
            ui.finish("CANCELLED")
            ui.wait_for_close()
            return 0
        except Exception as exc:
            ui.output(f"\n✗ {exc}")
            ui.finish("FAILED")
            ui.wait_for_close()
            return 2

        last_message = next(
            (message for message in reversed(ui.messages) if message.strip()),
            "",
        )
        ui.finish("CANCELLED" if last_message == "No changes made." else "COMPLETE")
        ui.wait_for_close()
        return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if not args.gamecore and terminal_supports_full_screen(input, print):
        try:
            return run_full_screen(args)
        except OSError:
            # If an unusual TTY refuses termios or the alternate buffer, retain
            # the complete line-oriented workflow instead of failing at launch.
            pass
    try:
        return run(args)
    except (
        InstallerError,
        gamecore.GameCoreError,
        best_mods.DownloadError,
        packer.PackError,
        ET.ParseError,
        OSError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
