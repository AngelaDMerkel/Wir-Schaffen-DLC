#!/usr/bin/env python3

import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import civ5_dlc_installer as INSTALLER


class InstallerTests(unittest.TestCase):
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
        self.assertIn("AngelaDMerkel's: WIR SCHAFFEN DLC", banner)
        self.assertIn("installer v0.1.0", banner)
        self.assertNotIn("\033", banner)
        self.assertEqual({len(line) for line in banner.splitlines()}, {54})

    def test_parse_selection_supports_numbers_ranges_and_all(self):
        self.assertEqual(INSTALLER.parse_selection("1, 3-4 2", 5), [0, 1, 2, 3])
        self.assertEqual(INSTALLER.parse_selection("all", 3), [0, 1, 2])
        with self.assertRaisesRegex(ValueError, "outside"):
            INSTALLER.parse_selection("4", 3)
        with self.assertRaisesRegex(ValueError, "ascending"):
            INSTALLER.parse_selection("3-1", 3)

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

            destinations = INSTALLER.install_packages([package], install.dlc_dir, replace=False)
            self.assertEqual((destinations[0] / "payload.txt").read_text(), "first")
            with self.assertRaisesRegex(INSTALLER.InstallerError, "already installed"):
                INSTALLER.install_packages([package], install.dlc_dir, replace=False)

            (source / "payload.txt").write_text("second", encoding="utf-8")
            INSTALLER.install_packages([package], install.dlc_dir, replace=True)
            self.assertEqual((destinations[0] / "payload.txt").read_text(), "second")

            with self.assertRaisesRegex(INSTALLER.InstallerError, "duplicate"):
                INSTALLER.install_packages([package, package], install.dlc_dir, replace=True)

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
