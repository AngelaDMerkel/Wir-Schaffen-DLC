#!/usr/bin/env python3

import json
import os
import struct
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import civ5_dlc_installer as INSTALLER


class InstallerTests(unittest.TestCase):
    def write_map(self, path: Path, world_size: str = "WORLDSIZE_COLOSSAL") -> None:
        blocks = (
            b"TERRAIN_GRASS\0",
            b"FEATURE_FOREST\0",
            b"",
            b"RESOURCE_WHEAT\0",
            b"",
            b"Installer Test Map\0",
            b"Test\0",
        )
        header = bytes((12,)) + struct.pack("<II", 12, 5) + bytes((6,))
        header += struct.pack("<I", 0) + struct.pack("<7I", *(len(block) for block in blocks))
        world = world_size.encode("ascii")
        plot = bytes((0, 0xFF, 0xFF, 0, 0, 0xFF, 0, 0))
        path.write_bytes(header + b"".join(blocks) + struct.pack("<I", len(world)) + world + plot * 60)

    def make_mod(self, parent: Path, folder: str, name: str = "Example Mod", version: int = 1) -> Path:
        mod = parent / folder
        mod.mkdir()
        (mod / "example.modinfo").write_text(
            f"""<Mod id="{uuid.uuid4()}" version="{version}">
  <Properties><Name>{name}</Name><Description>Test</Description></Properties>
  <Files />
</Mod>""",
            encoding="utf-8",
        )
        return mod

    def make_install(self, root: Path) -> INSTALLER.Civ5Install:
        app = root / "Civilization V.app"
        (app / "Contents/Assets/Assets/DLC").mkdir(parents=True)
        user_data = root / "User Data"
        (user_data / "MODS").mkdir(parents=True)
        (user_data / "Maps").mkdir()
        (user_data / "cache").mkdir()
        (user_data / "cache/Civ5CoreDatabase.db").write_bytes(b"database")
        return INSTALLER.validate_install(app, user_data)

    def prepared(self, mod: INSTALLER.InstalledMod, source: Path) -> INSTALLER.PreparedPackage:
        return INSTALLER.PreparedPackage(
            mod=mod,
            path=source,
            install_name=INSTALLER.package_install_name(mod),
            report=mock.Mock(),
        )

    def test_brand_banner_is_plain_without_terminal_color(self):
        banner = INSTALLER.terminal_banner(color=False)
        self.assertIn("ANGELADMERKEL'S:", banner)
        self.assertIn(" __      _____ ___", banner)
        self.assertIn("  ___  ___ _  _   _   ___ ___ ___ _  _", banner)
        self.assertIn("Nothing puts the I in team like me", banner)
        self.assertIn(f"installer · v{INSTALLER.packer.VERSION}", banner)
        self.assertIn("|___/|____\\___|", banner)
        self.assertNotIn("\033", banner)
        self.assertLessEqual(max(map(len, banner.splitlines())), 88)

    def test_brand_banner_and_menu_use_ansi_only_when_requested(self):
        self.assertIn("\033[", INSTALLER.terminal_banner(color=True))
        self.assertIn("\033[", INSTALLER.installation_mode_menu(color=True))
        self.assertIn(INSTALLER.TUI_YELLOW_BACKGROUND, INSTALLER.installation_mode_menu(color=True))
        self.assertIn(INSTALLER.TUI_SELECTED_FOREGROUND, INSTALLER.installation_mode_menu(color=True))
        self.assertNotIn("\033[", INSTALLER.installation_mode_menu(color=False))

    def test_bauhaus_palette_matches_the_approved_light_mockup(self):
        self.assertEqual(INSTALLER.TUI_BACKGROUND, "\033[48;2;251;244;221m")
        self.assertEqual(INSTALLER.TUI_FOREGROUND, "\033[38;2;31;38;60m")
        self.assertEqual(INSTALLER.TUI_MUTED, "\033[38;2;86;97;127m")
        self.assertEqual(INSTALLER.TUI_YELLOW, "\033[38;2;140;105;0m")
        self.assertEqual(INSTALLER.TUI_YELLOW_BACKGROUND, "\033[48;2;255;221;69m")
        self.assertEqual(INSTALLER.TUI_CYAN, "\033[38;2;33;111;130m")
        self.assertEqual(INSTALLER.TUI_RED, "\033[38;2;214;75;64m")

    def test_full_screen_frame_paints_and_centers_the_entire_terminal(self):
        size = os.terminal_size((120, 30))
        with mock.patch.object(INSTALLER.shutil, "get_terminal_size", return_value=size):
            colored = INSTALLER.full_screen_mode_frame(selected=0, color=True)
            plain = INSTALLER.full_screen_mode_frame(selected=0, color=False)
        self.assertTrue(colored.startswith(INSTALLER.TUI_BACKGROUND + INSTALLER.TUI_FOREGROUND))
        painted_rows = colored.split("\033[H", 1)[1].splitlines()
        self.assertEqual(len(painted_rows), 30)
        self.assertTrue(
            all(INSTALLER.terminal_display_width(row) == 120 for row in painted_rows)
        )
        self.assertNotIn("\033[K", colored)
        self.assertIn(INSTALLER.TUI_YELLOW_BACKGROUND, colored)
        self.assertIn(INSTALLER.RESTORE_STOCK_NAME, plain)
        self.assertIn("[↑/↓] move", plain)
        select_row = next(line for line in plain.splitlines() if "SELECT PROGRAM" in line)
        self.assertTrue(select_row.startswith(" " * 16))

    def test_workflow_frame_keeps_progress_and_prompts_inside_the_theme(self):
        size = os.terminal_size((120, 30))
        ui = INSTALLER.FullScreenTerminalUI(color=True)
        ui.program = INSTALLER.PROGRAM_LABELS["excogitare-patch"]
        ui.messages = [
            "→ Building Excogitare map-size compatibility DLC...",
            "  ✓ Extreme, Colossal, and custom-geometry metadata ready.",
        ]
        ui.progress = INSTALLER.TerminalProgress(
            phase="DOWNLOAD",
            item="Mass Effect Civilizations",
            position=3,
            total_items=7,
            completed=5 * 1024 * 1024,
            total=10 * 1024 * 1024,
            bytes_per_second=2 * 1024 * 1024,
            detail="Receiving Workshop archive",
            byte_progress=True,
        )
        ui.prompt_text = "Install the compatibility DLC and patch Civilization V? [y/N]:"
        ui.input_buffer = "y"
        with mock.patch.object(INSTALLER.shutil, "get_terminal_size", return_value=size):
            frame = ui.frame()
        painted_rows = frame.split("\033[H", 1)[1].splitlines()
        self.assertEqual(len(painted_rows), 30)
        self.assertTrue(
            all(INSTALLER.terminal_display_width(row) == 120 for row in painted_rows)
        )
        self.assertIn("PROGRAM 03", frame)
        self.assertIn("Building Excogitare", frame)
        self.assertIn("Install the compatibility DLC", frame)
        self.assertIn("[DOWNLOAD]", frame)
        self.assertIn("Mass Effect Civilizations", frame)
        self.assertIn("50%", frame)
        self.assertIn("2.0 MiB/s", frame)
        self.assertIn(INSTALLER.TUI_YELLOW_BACKGROUND, frame)

    def test_full_screen_runner_holds_the_session_through_completion_and_errors(self):
        args = INSTALLER.make_parser().parse_args([])
        ui = mock.Mock()
        ui.input = mock.Mock()
        ui.output = mock.Mock()
        ui.messages = ["✓ Installed successfully"]
        screen = mock.MagicMock()
        screen.__enter__.return_value = ui
        with mock.patch.object(
            INSTALLER,
            "FullScreenTerminalUI",
            return_value=screen,
        ), mock.patch.object(INSTALLER, "terminal_supports_color", return_value=True), mock.patch.object(
            INSTALLER,
            "run",
            return_value=0,
        ) as run:
            self.assertEqual(INSTALLER.run_full_screen(args), 0)
        run.assert_called_once_with(
            args,
            input_fn=ui.input,
            output_fn=ui.output,
            terminal_ui=ui,
        )
        ui.finish.assert_called_once_with("COMPLETE")
        ui.wait_for_close.assert_called_once_with()
        screen.__exit__.assert_called_once()

        failed_ui = mock.Mock()
        failed_ui.input = mock.Mock()
        failed_ui.output = mock.Mock()
        failed_ui.messages = []
        failed_screen = mock.MagicMock()
        failed_screen.__enter__.return_value = failed_ui
        with mock.patch.object(
            INSTALLER,
            "FullScreenTerminalUI",
            return_value=failed_screen,
        ), mock.patch.object(INSTALLER, "terminal_supports_color", return_value=True), mock.patch.object(
            INSTALLER,
            "run",
            side_effect=INSTALLER.InstallerError("test failure"),
        ):
            self.assertEqual(INSTALLER.run_full_screen(args), 2)
        failed_ui.output.assert_called_once_with("\n✗ test failure")
        failed_ui.finish.assert_called_once_with("FAILED")
        failed_ui.wait_for_close.assert_called_once_with()

    def test_installation_menu_highlight_moves_with_selection(self):
        first = INSTALLER.installation_mode_menu(color=False, selected=0, full_screen=True)
        second = INSTALLER.installation_mode_menu(color=False, selected=1, full_screen=True)
        third = INSTALLER.installation_mode_menu(color=False, selected=2, full_screen=True)
        fourth = INSTALLER.installation_mode_menu(color=False, selected=3, full_screen=True)
        self.assertIn("▶ 01", first)
        self.assertIn("REMOTE", first)
        self.assertIn("Download seven verified mods", first)
        self.assertNotIn("EXCLUSIVE", first)
        self.assertIn("  02", first)
        self.assertIn("  01", second)
        self.assertIn("▶ 02", second)
        self.assertIn("▶ 03", third)
        self.assertIn("Install AngelaDMerkel's map patch", third)
        self.assertIn("▶ 04", fourth)
        self.assertIn(INSTALLER.RESTORE_STOCK_NAME, fourth)
        self.assertIn("[↑/↓] move", second)
        self.assertNotIn("■ SOURCE", first)
        self.assertNotIn("● VERIFY", first)
        self.assertNotIn("▲ PACKAGE", first)
        self.assertNotIn("◆ INSTALL", first)
        with self.assertRaisesRegex(ValueError, "invalid installation-mode selection"):
            INSTALLER.installation_mode_menu(selected=4)

    def test_terminal_key_decoder_supports_arrows_enter_shortcuts_and_quit(self):
        self.assertEqual(INSTALLER.decode_terminal_key(b"\x1b[A"), "up")
        self.assertEqual(INSTALLER.decode_terminal_key(b"\x1b[B"), "down")
        self.assertEqual(INSTALLER.decode_terminal_key(b"\r"), "select")
        self.assertEqual(INSTALLER.decode_terminal_key(b"1"), "first")
        self.assertEqual(INSTALLER.decode_terminal_key(b"2"), "second")
        self.assertEqual(INSTALLER.decode_terminal_key(b"3"), "third")
        self.assertEqual(INSTALLER.decode_terminal_key(b"4"), "fourth")
        self.assertEqual(INSTALLER.decode_terminal_key(b"q"), "quit")
        self.assertIsNone(INSTALLER.decode_terminal_key(b"x"))

    @unittest.skipIf(INSTALLER.termios is None or INSTALLER.tty is None, "termios unavailable")
    def test_alternate_screen_restores_terminal_after_an_error(self):
        stdin = mock.Mock()
        stdin.fileno.return_value = 42
        stdout = mock.Mock()
        with mock.patch.object(INSTALLER.sys, "stdin", stdin), mock.patch.object(
            INSTALLER.sys, "stdout", stdout
        ), mock.patch.object(INSTALLER.termios, "tcgetattr", return_value=["saved"]), mock.patch.object(
            INSTALLER.termios, "tcsetattr"
        ) as restore, mock.patch.object(INSTALLER.tty, "setcbreak") as cbreak:
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                with INSTALLER.alternate_terminal_screen(color=True) as file_descriptor:
                    self.assertEqual(file_descriptor, 42)
                    raise RuntimeError("test failure")
        cbreak.assert_called_once_with(42, INSTALLER.termios.TCSANOW)
        restore.assert_called_once_with(42, INSTALLER.termios.TCSANOW, ["saved"])
        written = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("\033[?1049h", written)
        self.assertIn("\033[?1049l", written)
        self.assertIn("\033[?7l", written)
        self.assertIn("\033[?7h", written)
        self.assertIn(INSTALLER.TUI_SET_DEFAULT_BACKGROUND, written)
        self.assertIn(INSTALLER.TUI_RESET_DEFAULT_BACKGROUND, written)

    def test_source_mod_conflict_warning_explains_the_safe_launch_path(self):
        warning = INSTALLER.source_mod_conflict_warning()
        self.assertIn("standard Multiplayer", warning)
        self.assertIn("Do not enable", warning)
        self.assertIn("loads the same gameplay twice", warning)

    def test_package_install_name_is_ascii_for_legacy_dlc_scanner(self):
        with tempfile.TemporaryDirectory() as temp:
            mods_dir = Path(temp)
            self.make_mod(mods_dir, "Map", name="Encircled Séas — trial")
            mod = INSTALLER.discover_mods(mods_dir)[0][0]
            name = INSTALLER.package_install_name(mod)
            self.assertTrue(name.isascii())
            self.assertIn("Encircled Seas - trial", name)

    def test_excogitare_sizes_and_extreme_geometries_require_the_engine_guard(self):
        custom_size = mock.Mock(
            map_info=mock.Mock(
                world_size="WORLDSIZE_EXTREME",
                width=180,
                height=94,
                aspect_ratio=180 / 94,
            )
        )
        extreme_geometry = mock.Mock(
            map_info=mock.Mock(
                world_size="WORLDSIZE_HUGE",
                width=350,
                height=29,
                aspect_ratio=350 / 29,
            )
        )
        ordinary = mock.Mock(
            map_info=mock.Mock(
                world_size="WORLDSIZE_HUGE",
                width=128,
                height=80,
                aspect_ratio=128 / 80,
            )
        )
        self.assertTrue(INSTALLER.map_requires_engine_patch(custom_size))
        self.assertTrue(INSTALLER.map_requires_engine_patch(extreme_geometry))
        self.assertFalse(INSTALLER.map_requires_engine_patch(ordinary))

    def test_colossal_engine_patch_is_exact_and_reversible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game_app = root / "Civilization V.app"
            executable = game_app / INSTALLER.ENGINE_RELATIVE_PATH
            executable.parent.mkdir(parents=True)
            payload = bytearray(INSTALLER.ENGINE_SPAN_PATCH_OFFSET + 128)
            for start, original, _ in INSTALLER.ENGINE_PATCH_SITES:
                payload[start : start + len(original)] = original
            executable.write_bytes(payload)
            user_data = root / "User Data"
            user_data.mkdir()

            original_hash = INSTALLER.file_sha256(executable)
            with mock.patch.object(INSTALLER, "ENGINE_ORIGINAL_SHA256", original_hash):
                with mock.patch.object(INSTALLER.subprocess, "run") as signed:
                    self.assertTrue(INSTALLER.apply_colossal_engine_patch(game_app, user_data))
                    self.assertEqual(INSTALLER.engine_patch_state(executable), "patched")
                    for start, _, patched in INSTALLER.ENGINE_PATCH_SITES:
                        self.assertEqual(
                            executable.read_bytes()[start : start + len(patched)], patched
                        )
                    self.assertEqual(signed.call_count, 2)
                self.assertFalse(INSTALLER.apply_colossal_engine_patch(game_app, user_data))
                self.assertTrue(INSTALLER.restore_colossal_engine_patch(game_app, user_data))
            self.assertEqual(INSTALLER.engine_patch_state(executable), "original")

    def test_colossal_engine_patch_upgrades_divisor_only_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game_app = root / "Civilization V.app"
            executable = game_app / INSTALLER.ENGINE_RELATIVE_PATH
            executable.parent.mkdir(parents=True)
            payload = bytearray(INSTALLER.ENGINE_SPAN_PATCH_OFFSET + 128)
            for start, original, _ in INSTALLER.ENGINE_PATCH_SITES:
                payload[start : start + len(original)] = original
            executable.write_bytes(payload)
            original_hash = INSTALLER.file_sha256(executable)

            user_data = root / "User Data"
            backup = INSTALLER.engine_backup_path(user_data)
            backup.parent.mkdir(parents=True)
            backup.write_bytes(payload)

            current = bytearray(payload)
            start = INSTALLER.ENGINE_PATCH_OFFSET
            current[start : start + len(INSTALLER.ENGINE_PATCHED_BYTES)] = (
                INSTALLER.ENGINE_PATCHED_BYTES
            )
            executable.write_bytes(current)
            divisor_only_hash = INSTALLER.file_sha256(executable)
            self.assertEqual(INSTALLER.engine_patch_state(executable), "divisor_guard")

            with mock.patch.object(INSTALLER, "ENGINE_ORIGINAL_SHA256", original_hash):
                with mock.patch.object(
                    INSTALLER, "ENGINE_DIVISOR_ONLY_SHA256", divisor_only_hash
                ):
                    with mock.patch.object(INSTALLER.subprocess, "run"):
                        self.assertTrue(
                            INSTALLER.apply_colossal_engine_patch(game_app, user_data)
                        )
            self.assertEqual(INSTALLER.engine_patch_state(executable), "patched")

    def test_colossal_engine_patch_upgrades_zero_span_test_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game_app = root / "Civilization V.app"
            executable = game_app / INSTALLER.ENGINE_RELATIVE_PATH
            executable.parent.mkdir(parents=True)
            payload = bytearray(INSTALLER.ENGINE_SPAN_PATCH_OFFSET + 128)
            for start, original, _ in INSTALLER.ENGINE_PATCH_SITES:
                payload[start : start + len(original)] = original
            executable.write_bytes(payload)
            original_hash = INSTALLER.file_sha256(executable)

            user_data = root / "User Data"
            backup = INSTALLER.engine_backup_path(user_data)
            backup.parent.mkdir(parents=True)
            backup.write_bytes(payload)

            current = bytearray(payload)
            start = INSTALLER.ENGINE_PATCH_OFFSET
            current[start : start + len(INSTALLER.ENGINE_PATCHED_BYTES)] = (
                INSTALLER.ENGINE_PATCHED_BYTES
            )
            start = INSTALLER.ENGINE_SPAN_PATCH_OFFSET
            current[start : start + len(INSTALLER.ENGINE_ZERO_SPAN_BYTES)] = (
                INSTALLER.ENGINE_ZERO_SPAN_BYTES
            )
            executable.write_bytes(current)
            zero_span_hash = INSTALLER.file_sha256(executable)
            self.assertEqual(INSTALLER.engine_patch_state(executable), "zero_span_guard")

            with mock.patch.object(INSTALLER, "ENGINE_ORIGINAL_SHA256", original_hash):
                with mock.patch.object(INSTALLER, "ENGINE_ZERO_SPAN_SHA256", zero_span_hash):
                    with mock.patch.object(INSTALLER.subprocess, "run"):
                        self.assertTrue(
                            INSTALLER.apply_colossal_engine_patch(game_app, user_data)
                        )
            self.assertEqual(INSTALLER.engine_patch_state(executable), "patched")

    def test_colossal_engine_patch_upgrades_bounded_fallback_test_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game_app = root / "Civilization V.app"
            executable = game_app / INSTALLER.ENGINE_RELATIVE_PATH
            executable.parent.mkdir(parents=True)
            payload = bytearray(INSTALLER.ENGINE_SPAN_PATCH_OFFSET + 128)
            for start, original, _ in INSTALLER.ENGINE_PATCH_SITES:
                payload[start : start + len(original)] = original
            executable.write_bytes(payload)
            original_hash = INSTALLER.file_sha256(executable)

            user_data = root / "User Data"
            backup = INSTALLER.engine_backup_path(user_data)
            backup.parent.mkdir(parents=True)
            backup.write_bytes(payload)

            current = bytearray(payload)
            start = INSTALLER.ENGINE_PATCH_OFFSET
            current[start : start + len(INSTALLER.ENGINE_PATCHED_BYTES)] = (
                INSTALLER.ENGINE_PATCHED_BYTES
            )
            start = INSTALLER.ENGINE_SPAN_PATCH_OFFSET
            current[start : start + len(INSTALLER.ENGINE_BOUNDED_FALLBACK_BYTES)] = (
                INSTALLER.ENGINE_BOUNDED_FALLBACK_BYTES
            )
            executable.write_bytes(current)
            bounded_hash = INSTALLER.file_sha256(executable)
            self.assertEqual(
                INSTALLER.engine_patch_state(executable), "bounded_fallback_guard"
            )

            with mock.patch.object(INSTALLER, "ENGINE_ORIGINAL_SHA256", original_hash):
                with mock.patch.object(
                    INSTALLER, "ENGINE_BOUNDED_FALLBACK_SHA256", bounded_hash
                ):
                    with mock.patch.object(INSTALLER.subprocess, "run"):
                        self.assertTrue(
                            INSTALLER.apply_colossal_engine_patch(game_app, user_data)
                        )
            self.assertEqual(INSTALLER.engine_patch_state(executable), "patched")

    def test_parse_selection_supports_numbers_ranges_and_all(self):
        self.assertEqual(INSTALLER.parse_selection("1, 3-4 2", 5), [0, 1, 2, 3])
        self.assertEqual(INSTALLER.parse_selection("all", 3), [0, 1, 2])
        with self.assertRaisesRegex(ValueError, "outside"):
            INSTALLER.parse_selection("4", 3)
        with self.assertRaisesRegex(ValueError, "ascending"):
            INSTALLER.parse_selection("3-1", 3)

    def test_top_level_programs_and_exclusive_arguments(self):
        self.assertEqual(
            INSTALLER.choose_install_mode(True, None, [], lambda _: "", lambda _: None),
            "very-best-mods",
        )
        self.assertEqual(
            INSTALLER.choose_install_mode(False, None, [], lambda _: "1", lambda _: None),
            "very-best-mods",
        )
        self.assertEqual(
            INSTALLER.choose_install_mode(False, None, [], lambda _: "", lambda _: None),
            "very-best-mods",
        )
        self.assertEqual(
            INSTALLER.choose_install_mode(False, None, [], lambda _: "2", lambda _: None),
            "installed",
        )
        self.assertEqual(
            INSTALLER.choose_install_mode(False, None, [], lambda _: "3", lambda _: None),
            "excogitare-patch",
        )
        self.assertEqual(
            INSTALLER.choose_install_mode(False, None, [], lambda _: "4", lambda _: None),
            "restore-stock",
        )
        self.assertEqual(
            INSTALLER.choose_install_mode(
                False,
                None,
                [],
                lambda _: "",
                lambda _: None,
                excogitare_patch=True,
            ),
            "excogitare-patch",
        )
        self.assertEqual(
            INSTALLER.choose_install_mode(
                False,
                None,
                [],
                lambda _: "",
                lambda _: None,
                restore_stock=True,
            ),
            "restore-stock",
        )
        self.assertEqual(
            INSTALLER.choose_install_mode(False, "all", [], lambda _: "", lambda _: None),
            "installed",
        )
        with self.assertRaisesRegex(INSTALLER.InstallerError, "mutually exclusive"):
            INSTALLER.choose_install_mode(True, "all", [], lambda _: "", lambda _: None)
        with self.assertRaisesRegex(INSTALLER.InstallerError, "mutually exclusive"):
            INSTALLER.choose_install_mode(True, None, [Path("map.Civ5Map")], lambda _: "", lambda _: None)
        with self.assertRaisesRegex(INSTALLER.InstallerError, "mutually exclusive"):
            INSTALLER.choose_install_mode(
                False,
                None,
                [Path("map.Civ5Map")],
                lambda _: "",
                lambda _: None,
                excogitare_patch=True,
            )

        menu = INSTALLER.installation_mode_menu(color=False)
        self.assertLess(
            menu.index(INSTALLER.best_mods.PRESET_NAME),
            menu.index("Package installed mods & maps"),
        )
        self.assertLess(
            menu.index("Package installed mods & maps"),
            menu.index("Install AngelaDMerkel's map patch"),
        )
        self.assertLess(
            menu.index("Install AngelaDMerkel's map patch"),
            menu.index(INSTALLER.RESTORE_STOCK_NAME),
        )

        with mock.patch.object(
            INSTALLER, "terminal_supports_full_screen", return_value=True
        ), mock.patch.object(
            INSTALLER, "choose_install_mode_full_screen", return_value="installed"
        ) as chooser:
            self.assertEqual(
                INSTALLER.choose_install_mode(False, None, [], input, print, color=True),
                "installed",
            )
        chooser.assert_called_once_with(True)

    def test_quitting_from_landing_screen_does_not_print_wrapping_install_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            install = self.make_install(Path(temp))
            args = INSTALLER.make_parser().parse_args(
                [
                    "--game-app",
                    str(install.game_app),
                    "--user-data",
                    str(install.user_data),
                ]
            )
            output: list[str] = []
            self.assertEqual(
                INSTALLER.run(args, input_fn=lambda _: "q", output_fn=output.append),
                0,
            )
            rendered = "\n".join(output)
            self.assertIn("SELECT PROGRAM", rendered)
            self.assertNotIn("\nGame:", rendered)
            self.assertNotIn("\nMods:", rendered)
            self.assertNotIn("\nDLC:", rendered)

    def test_persistent_ui_owns_mode_selection_and_suppresses_scrolling_banner(self):
        with tempfile.TemporaryDirectory() as temp:
            install = self.make_install(Path(temp))
            args = INSTALLER.make_parser().parse_args(
                [
                    "--game-app",
                    str(install.game_app),
                    "--user-data",
                    str(install.user_data),
                ]
            )

            class FakeUI:
                def __init__(self):
                    self.program = "SETUP"
                    self.messages: list[str] = []

                def choose_install_mode(self):
                    return "quit"

                def output(self, message=""):
                    self.messages.append(message)

                def set_program(self, mode):
                    self.program = mode

            ui = FakeUI()
            self.assertEqual(
                INSTALLER.run(
                    args,
                    input_fn=lambda _: "",
                    output_fn=ui.output,
                    terminal_ui=ui,
                ),
                0,
            )
            self.assertEqual(ui.messages, ["No changes made."])
            self.assertFalse(any("ANGELADMERKEL'S:" in line for line in ui.messages))

    def test_exclusive_collection_blocks_other_generated_packages_both_ways(self):
        with tempfile.TemporaryDirectory() as temp:
            dlc = Path(temp)
            preset = dlc / "Preset Member"
            preset.mkdir()
            (preset / "pack-report.json").write_text(
                json.dumps(
                    {
                        "source_id": INSTALLER.best_mods.SOURCES[0].manifest_id,
                        "collection": {"id": INSTALLER.best_mods.PRESET_ID},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(INSTALLER.InstallerError, "exclusive collection"):
                INSTALLER.validate_exclusive_mode(dlc, "installed")

            (preset / "pack-report.json").write_text(
                json.dumps({"source_id": str(uuid.uuid4())}), encoding="utf-8"
            )
            with self.assertRaisesRegex(INSTALLER.InstallerError, "cannot coexist"):
                INSTALLER.validate_exclusive_mode(dlc, "very-best-mods")

            (preset / "pack-report.json").write_text(
                json.dumps({"source_id": INSTALLER.EXCOGITARE_PATCH_ID}),
                encoding="utf-8",
            )
            INSTALLER.validate_exclusive_mode(dlc, "very-best-mods")

    def test_builds_excogitare_size_compatibility_as_an_authenticated_dlc(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            discovered = self.make_install(root)
            install = INSTALLER.Civ5Install(
                game_app=discovered.game_app,
                user_data=discovered.user_data,
                game_assets=discovered.game_assets,
                dlc_dir=discovered.dlc_dir,
                mods_dir=discovered.mods_dir,
                maps_dir=discovered.maps_dir,
                base_db=None,
            )
            staging = root / "staging"
            staging.mkdir()
            package = INSTALLER.build_excogitare_patch_package(
                install,
                staging,
                lambda _: None,
            )
            worlds = ET.parse(package.path / package.report.game_data[0]).getroot()
            rows = {
                row.findtext("Type"): row
                for row in worlds.findall("./Worlds/Replace")
            }
            self.assertEqual(set(rows), INSTALLER.EXCOGITARE_CUSTOM_WORLD_TYPES)
            self.assertEqual(rows["WORLDSIZE_EXTREME"].findtext("GridWidth"), "180")
            self.assertEqual(rows["WORLDSIZE_EXTREME"].findtext("DefaultMinorCivs"), "20")
            self.assertEqual(rows["WORLDSIZE_COLOSSAL"].findtext("GridHeight"), "110")
            self.assertEqual(rows["WORLDSIZE_COLOSSAL"].findtext("DefaultPlayers"), "22")
            report = json.loads((package.path / "pack-report.json").read_text())
            self.assertEqual(report["source_kind"], "ExcogitareMapPatch")
            self.assertEqual(report["source_id"], INSTALLER.EXCOGITARE_PATCH_ID)
            self.assertEqual(len(report["world_sizes"]), 2)
            self.assertTrue(next(package.path.glob("*.Civ5Pkg")).is_file())

    def test_excogitare_program_installs_the_dlc_and_reversible_engine_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.make_install(root)
            if install.base_db is not None:
                install.base_db.unlink()
            executable = install.game_app / INSTALLER.ENGINE_RELATIVE_PATH
            executable.parent.mkdir(parents=True, exist_ok=True)
            payload = bytearray(INSTALLER.ENGINE_SPAN_PATCH_OFFSET + 128)
            for start, original, _ in INSTALLER.ENGINE_PATCH_SITES:
                payload[start : start + len(original)] = original
            executable.write_bytes(payload)
            original_hash = INSTALLER.file_sha256(executable)
            args = INSTALLER.make_parser().parse_args(
                [
                    "--game-app",
                    str(install.game_app),
                    "--user-data",
                    str(install.user_data),
                    "--excogitare-patch",
                    "--yes",
                ]
            )
            output: list[str] = []
            with mock.patch.object(
                INSTALLER,
                "ENGINE_ORIGINAL_SHA256",
                original_hash,
            ), mock.patch.object(INSTALLER.subprocess, "run"):
                self.assertEqual(INSTALLER.run(args, output_fn=output.append), 0)
            self.assertEqual(INSTALLER.engine_patch_state(executable), "patched")
            reports = list(install.dlc_dir.glob("*/pack-report.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text())
            self.assertEqual(report["source_id"], INSTALLER.EXCOGITARE_PATCH_ID)
            self.assertIn("Excogitare map compatibility installed successfully", "\n".join(output))

    def test_restore_stock_removes_only_verified_tool_changes_and_rebuildable_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            discovered = self.make_install(root)
            install_without_db = INSTALLER.Civ5Install(
                game_app=discovered.game_app,
                user_data=discovered.user_data,
                game_assets=discovered.game_assets,
                dlc_dir=discovered.dlc_dir,
                mods_dir=discovered.mods_dir,
                maps_dir=discovered.maps_dir,
                base_db=None,
            )
            staging = root / "staging"
            staging.mkdir()
            package = INSTALLER.build_excogitare_patch_package(
                install_without_db,
                staging,
                lambda _: None,
            )
            installed = INSTALLER.install_packages(
                [package],
                discovered.dlc_dir,
                replace=False,
            )[0]
            official = discovered.dlc_dir / "DLC_Official_Test"
            official.mkdir()
            (official / "official.txt").write_text("preserve", encoding="utf-8")
            user_mod = self.make_mod(discovered.mods_dir, "Preserved User Mod")
            user_map = discovered.maps_dir / "preserved.Civ5Map"
            user_map.write_bytes(b"preserve")
            cache_dir = discovered.user_data / "cache"
            (cache_dir / "Civ5DebugDatabase.db").write_bytes(b"rebuild")
            preserved_cache = cache_dir / "Civ5SavedGameDatabase.db"
            preserved_cache.write_bytes(b"preserve")

            executable = discovered.game_app / INSTALLER.ENGINE_RELATIVE_PATH
            executable.parent.mkdir(parents=True, exist_ok=True)
            payload = bytearray(INSTALLER.ENGINE_SPAN_PATCH_OFFSET + 128)
            for start, original, _ in INSTALLER.ENGINE_PATCH_SITES:
                payload[start : start + len(original)] = original
            executable.write_bytes(payload)
            original_hash = INSTALLER.file_sha256(executable)
            args = INSTALLER.make_parser().parse_args(
                [
                    "--game-app",
                    str(discovered.game_app),
                    "--user-data",
                    str(discovered.user_data),
                    "--restore-stock",
                    "--yes",
                ]
            )
            output: list[str] = []
            with mock.patch.object(
                INSTALLER,
                "ENGINE_ORIGINAL_SHA256",
                original_hash,
            ), mock.patch.object(INSTALLER.subprocess, "run"):
                self.assertTrue(
                    INSTALLER.apply_colossal_engine_patch(
                        discovered.game_app,
                        discovered.user_data,
                    )
                )
                self.assertEqual(INSTALLER.run(args, output_fn=output.append), 0)

            self.assertFalse(installed.exists())
            self.assertEqual((official / "official.txt").read_text(), "preserve")
            self.assertTrue(user_mod.is_dir())
            self.assertEqual(user_map.read_bytes(), b"preserve")
            self.assertEqual(INSTALLER.file_sha256(executable), original_hash)
            self.assertEqual(INSTALLER.engine_patch_state(executable), "original")
            self.assertFalse((cache_dir / "Civ5CoreDatabase.db").exists())
            self.assertFalse((cache_dir / "Civ5DebugDatabase.db").exists())
            self.assertEqual(preserved_cache.read_bytes(), b"preserve")
            self.assertFalse(INSTALLER.engine_backup_path(discovered.user_data).exists())
            self.assertIn("Stock Civilization V installation restored", "\n".join(output))

    def test_restore_stock_refuses_an_unverifiable_generated_dlc_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.make_install(root)
            suspicious = install.dlc_dir / "Civ5MP - Unverifiable"
            suspicious.mkdir()
            with self.assertRaisesRegex(INSTALLER.InstallerError, "ownership"):
                INSTALLER.stock_revert_plan(install)
            self.assertTrue(suspicious.is_dir())

    def test_restore_stock_rolls_packages_back_if_engine_restoration_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            discovered = self.make_install(root)
            install_without_db = INSTALLER.Civ5Install(
                game_app=discovered.game_app,
                user_data=discovered.user_data,
                game_assets=discovered.game_assets,
                dlc_dir=discovered.dlc_dir,
                mods_dir=discovered.mods_dir,
                maps_dir=discovered.maps_dir,
                base_db=None,
            )
            staging = root / "staging"
            staging.mkdir()
            package = INSTALLER.build_excogitare_patch_package(
                install_without_db, staging, lambda _: None
            )
            installed = INSTALLER.install_packages(
                [package], discovered.dlc_dir, replace=False
            )[0]
            executable = discovered.game_app / INSTALLER.ENGINE_RELATIVE_PATH
            executable.parent.mkdir(parents=True, exist_ok=True)
            payload = bytearray(INSTALLER.ENGINE_SPAN_PATCH_OFFSET + 128)
            for start, original, _ in INSTALLER.ENGINE_PATCH_SITES:
                payload[start : start + len(original)] = original
            executable.write_bytes(payload)
            original_hash = INSTALLER.file_sha256(executable)
            args = INSTALLER.make_parser().parse_args(["--restore-stock", "--yes"])
            with mock.patch.object(
                INSTALLER, "ENGINE_ORIGINAL_SHA256", original_hash
            ), mock.patch.object(INSTALLER.subprocess, "run"):
                INSTALLER.apply_colossal_engine_patch(
                    discovered.game_app, discovered.user_data
                )
                with mock.patch.object(
                    INSTALLER,
                    "restore_colossal_engine_patch",
                    side_effect=INSTALLER.InstallerError("restore failed"),
                ):
                    with self.assertRaisesRegex(INSTALLER.InstallerError, "restore failed"):
                        INSTALLER.restore_stock_installation(
                            discovered,
                            args,
                            output_fn=lambda _: None,
                        )
            self.assertTrue(installed.is_dir())
            self.assertEqual(INSTALLER.engine_patch_state(executable), "patched")
            self.assertTrue((discovered.user_data / "cache/Civ5CoreDatabase.db").is_file())

    def test_collection_stamp_is_written_to_every_package_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packages = []
            for index in range(2):
                source = root / str(index)
                source.mkdir()
                (source / "pack-report.json").write_text(
                    json.dumps({"source_id": str(index)}), encoding="utf-8"
                )
                packages.append(mock.Mock(path=source))
            metadata = {
                "id": INSTALLER.best_mods.PRESET_ID,
                "name": INSTALLER.best_mods.PRESET_NAME,
                "exclusive": True,
                "members": [],
            }
            INSTALLER.stamp_collection(packages, metadata)
            for package in packages:
                report = json.loads((package.path / "pack-report.json").read_text())
                self.assertEqual(report["collection"], metadata)

    def test_discovers_only_directories_with_valid_modinfo(self):
        with tempfile.TemporaryDirectory() as temp:
            mods_dir = Path(temp)
            self.make_mod(mods_dir, "Valid", name="A Valid Mod", version=2)
            invalid = mods_dir / "Broken"
            invalid.mkdir()
            (invalid / "two.modinfo").write_text("bad", encoding="utf-8")
            mods, skipped = INSTALLER.discover_mods(mods_dir)
            self.assertEqual([(mod.manifest.name, mod.manifest.version) for mod in mods], [("A Valid Mod", 2)])
            self.assertEqual(len(skipped), 1)

    def test_discovers_standalone_maps_with_custom_world_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "test.Civ5Map"
            self.write_map(source, "WORLDSIZE_EXTREME")
            maps, skipped = INSTALLER.discover_maps([root, source])
            self.assertEqual(skipped, [])
            self.assertEqual(len(maps), 1)
            self.assertEqual(maps[0].manifest.name, "Installer Test Map")
            self.assertEqual(maps[0].map_info.world_size, "WORLDSIZE_EXTREME")
            self.assertEqual((maps[0].map_info.width, maps[0].map_info.height), (12, 5))

    def test_builds_one_isolated_shared_ui_bridge_for_combined_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.make_install(root)
            ingame = install.game_assets / "DLC/Expansion2/UI/InGame/InGame.lua"
            ingame.parent.mkdir(parents=True)
            ingame.write_text("-- stock BNW InGame\n", encoding="utf-8")
            mod_path = self.make_mod(install.mods_dir, "Example")
            mod = INSTALLER.discover_mods(install.mods_dir)[0][0]
            package_path = root / "prepared"
            files = package_path / "Files"
            files.mkdir(parents=True)
            (files / "FirstAddin.lua").write_text("-- first", encoding="utf-8")
            report = INSTALLER.packer.BuildReport(
                source_mod=mod.manifest.name,
                source_id=mod.manifest.mod_id,
                source_version=mod.manifest.version,
                dlc_id=str(uuid.uuid4()),
                ui_set="Expansion2",
                game_data=[],
                text_data=[],
                imported_files=["FirstAddin.lua"],
                maps=[],
                entry_points=["InGameUIAddin:Lua/FirstAddin.lua"],
                translated_sql=[],
                omitted_features=[],
                warnings=[],
                validation=[],
            )
            (package_path / "pack-report.json").write_text(
                json.dumps(report.__dict__), encoding="utf-8"
            )
            prepared = INSTALLER.PreparedPackage(mod, package_path, "Example", report)
            staging = root / "staging"
            staging.mkdir()
            bridge = INSTALLER.build_shared_ui_bridge([prepared], install, staging, lambda _: None)
            self.assertIsNotNone(bridge)
            generated = (bridge.path / "UI/InGame.lua").read_text(encoding="utf-8")
            self.assertIn('ContextPtr:LoadNewContext("FirstAddin")', generated)
            self.assertNotIn('include("FirstAddin")', generated)
            package = ET.parse(next(bridge.path.glob("*.Civ5Pkg"))).getroot()
            self.assertEqual(package.findtext("Priority"), "1")
            metadata = json.loads((bridge.path / "pack-report.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["source_kind"], "SharedUIBridge")

    def test_rejects_cross_package_database_filename_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packages = []
            for index, content in enumerate(("<GameData><A /></GameData>", "<GameData><B /></GameData>")):
                package = root / f"package-{index}"
                package.mkdir()
                (package / "001_Shared_GameData.xml").write_text(content, encoding="utf-8")
                packages.append(
                    (
                        package,
                        {
                            "source_mod": f"Package {index}",
                            "game_data": ["001_Shared_GameData.xml"],
                            "text_data": [],
                        },
                    )
                )
            with self.assertRaisesRegex(INSTALLER.InstallerError, "virtual-file collision"):
                INSTALLER.validate_combined_namespace(packages)

    def test_discovers_the_standard_macos_install(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            app = home / "Library/Application Support/Steam" / INSTALLER.GAME_STEAM_PATH
            (app / "Contents/Assets/Assets/DLC").mkdir(parents=True)
            user_data = home / INSTALLER.USER_DATA_PATH
            (user_data / "MODS").mkdir(parents=True)
            self.assertEqual(INSTALLER.discover_game_apps(home), [app.resolve()])
            self.assertEqual(INSTALLER.discover_user_data_dirs(home), [user_data.resolve()])

    def test_installs_and_explicitly_replaces_a_generated_package(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.make_install(root)
            mod_path = self.make_mod(install.mods_dir, "Example")
            mod = INSTALLER.discover_mods(install.mods_dir)[0][0]
            source = root / "package"
            source.mkdir()
            (source / "payload.txt").write_text("first", encoding="utf-8")
            package = self.prepared(mod, source)

            progress: list[tuple[int, int, str, bool]] = []
            destinations = INSTALLER.install_packages(
                [package],
                install.dlc_dir,
                replace=False,
                progress_fn=lambda position, total, title, complete: progress.append(
                    (position, total, title, complete)
                ),
            )
            self.assertEqual((destinations[0] / "payload.txt").read_text(), "first")
            self.assertEqual(progress[0], (1, 1, mod.manifest.name, False))
            self.assertEqual(progress[-1], (1, 1, mod.manifest.name, True))
            with self.assertRaisesRegex(INSTALLER.InstallerError, "already installed"):
                INSTALLER.install_packages([package], install.dlc_dir, replace=False)

            (source / "payload.txt").write_text("second", encoding="utf-8")
            INSTALLER.install_packages([package], install.dlc_dir, replace=True)
            self.assertEqual((destinations[0] / "payload.txt").read_text(), "second")

            with self.assertRaisesRegex(INSTALLER.InstallerError, "duplicate"):
                INSTALLER.install_packages([package, package], install.dlc_dir, replace=True)

    def test_replaces_a_legacy_package_with_the_same_source_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.make_install(root)
            self.make_mod(install.mods_dir, "Example")
            mod = INSTALLER.discover_mods(install.mods_dir)[0][0]
            source = root / "package"
            source.mkdir()
            (source / "payload.txt").write_text("fixed", encoding="utf-8")
            package = self.prepared(mod, source)

            legacy = install.dlc_dir / "Example Mod (v 1)"
            legacy.mkdir()
            (legacy / "pack-report.json").write_text(
                json.dumps(
                    {
                        "source_id": mod.manifest.mod_id,
                        "source_version": mod.manifest.version,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(INSTALLER.InstallerError, "already installed"):
                INSTALLER.install_packages([package], install.dlc_dir, replace=False)
            destinations = INSTALLER.install_packages([package], install.dlc_dir, replace=True)
            self.assertFalse(legacy.exists())
            self.assertEqual((destinations[0] / "payload.txt").read_text(), "fixed")

    def test_restores_legacy_package_if_final_rename_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.make_install(root)
            self.make_mod(install.mods_dir, "Example")
            mod = INSTALLER.discover_mods(install.mods_dir)[0][0]
            source = root / "package"
            source.mkdir()
            (source / "payload.txt").write_text("fixed", encoding="utf-8")
            package = self.prepared(mod, source)
            legacy = install.dlc_dir / "Legacy"
            legacy.mkdir()
            (legacy / "pack-report.json").write_text(
                json.dumps(
                    {"source_id": mod.manifest.mod_id, "source_version": mod.manifest.version}
                ),
                encoding="utf-8",
            )

            real_rename = Path.rename

            def fail_incoming(path, target):
                if path.name.startswith(".civ5-mod-dlc-incoming-"):
                    raise OSError("simulated final rename failure")
                return real_rename(path, target)

            with mock.patch.object(Path, "rename", autospec=True, side_effect=fail_incoming):
                with self.assertRaisesRegex(OSError, "simulated final rename failure"):
                    INSTALLER.install_packages([package], install.dlc_dir, replace=True)
            self.assertTrue(legacy.is_dir())
            self.assertFalse((install.dlc_dir / package.install_name).exists())
            self.assertFalse(any(path.name.startswith(".civ5-mod-dlc-") for path in install.dlc_dir.iterdir()))

    def test_rolls_back_when_a_multi_package_install_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.make_install(root)
            first_path = self.make_mod(install.mods_dir, "One", name="One")
            second_path = self.make_mod(install.mods_dir, "Two", name="Two")
            mods, _ = INSTALLER.discover_mods(install.mods_dir)
            sources = []
            for number in range(2):
                source = root / f"package-{number}"
                source.mkdir()
                (source / "payload.txt").write_text(str(number), encoding="utf-8")
                sources.append(source)
            packages = [self.prepared(mod, source) for mod, source in zip(mods, sources)]
            real_copytree = INSTALLER.shutil.copytree
            call_count = 0

            def fail_second_copy(source, destination):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError("simulated copy failure")
                return real_copytree(source, destination)

            with mock.patch.object(INSTALLER.shutil, "copytree", side_effect=fail_second_copy):
                with self.assertRaisesRegex(OSError, "simulated"):
                    INSTALLER.install_packages(packages, install.dlc_dir, replace=False)
            self.assertFalse(any((install.dlc_dir / package.install_name).exists() for package in packages))
            self.assertFalse(any(path.name.startswith(".civ5-mod-dlc-") for path in install.dlc_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
