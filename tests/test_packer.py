#!/usr/bin/env python3

import argparse
import json
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import civ5_dlc_packer as PACKER


class PackerTests(unittest.TestCase):
    def write_manifest(
        self,
        mod: Path,
        files: str = "",
        actions: str = "",
        entries: str = "",
    ) -> None:
        mod_id = uuid.UUID("aa1208a8-80db-499c-b334-5d71c45c241b")
        (mod / "example.modinfo").write_text(
            f"""<Mod id="{mod_id}" version="1">
  <Properties><Name>Example</Name><Description>Example mod</Description></Properties>
  <Files>{files}</Files>
  <Actions><OnModActivated>{actions}</OnModActivated></Actions>
  <EntryPoints>{entries}</EntryPoints>
</Mod>""",
            encoding="utf-8",
        )

    def fixture(self, root: Path, sql: bool = False, collision: bool = False) -> tuple[Path, Path]:
        mod = root / "Example Mod"
        mod.mkdir()
        (mod / "data.xml").write_text(
            "<GameData><Things><Row><Type>THING_ONE</Type></Row></Things>"
            "<Language_EN_US><Row Tag='TXT_KEY_THING'><Text>Thing</Text></Row></Language_EN_US></GameData>",
            encoding="utf-8",
        )
        files = ["<File import='0'>data.xml</File>"]
        action = "<UpdateDatabase>data.sql</UpdateDatabase>" if sql else "<UpdateDatabase>data.xml</UpdateDatabase>"
        if sql:
            (mod / "data.sql").write_text("INSERT INTO Things(Type) VALUES('THING_ONE');", encoding="utf-8")
            files.append("<File import='0'>data.sql</File>")
        if collision:
            (mod / "A").mkdir()
            (mod / "B").mkdir()
            (mod / "A/shared.dds").write_bytes(b"first")
            (mod / "B/shared.dds").write_bytes(b"second")
            (mod / "B/model.fxsxml").write_text('<Texture file="shared.dds"/>', encoding="utf-8")
            files.extend(
                [
                    "<File import='1'>A/shared.dds</File>",
                    "<File import='1'>B/shared.dds</File>",
                    "<File import='1'>B/model.fxsxml</File>",
                ]
            )
        self.write_manifest(mod, "".join(files), action)
        assets = root / "Assets"
        (assets / "DLC/Expansion2/UI/InGame").mkdir(parents=True)
        (assets / "DLC/Expansion2/UI/InGame/InGame.lua").write_text("-- InGame", encoding="utf-8")
        return mod, assets

    def args(self, mod: Path, output: Path, assets: Path) -> argparse.Namespace:
        return argparse.Namespace(
            mod=mod,
            output=output,
            base_db=None,
            game_assets=assets,
            ui_set="Expansion2",
            priority=10,
            force=False,
        )

    def test_packs_and_splits_game_and_text_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mod, assets = self.fixture(root)
            output = root / "output"
            report = PACKER.pack(self.args(mod, output, assets))
            self.assertEqual(len(report.game_data), 1)
            self.assertEqual(len(report.text_data), 1)
            package = next(output.glob("*.Civ5Pkg"))
            package_root = ET.parse(package).getroot()
            self.assertEqual(package_root.findtext("./Gameplay/GameData"), report.game_data[0])
            self.assertEqual(package_root.findtext("./Gameplay/TextData"), report.text_data[0])
            self.assertEqual(json.loads((output / "pack-report.json").read_text())["source_mod"], "Example")

    def test_refuses_untranslated_sql(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mod, assets = self.fixture(root, sql=True)
            with self.assertRaisesRegex(PACKER.PackError, "SQL cannot be loaded directly"):
                PACKER.pack(self.args(mod, root / "output", assets))

    def test_renames_different_flat_namespace_collisions_and_rewrites_sibling_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mod, assets = self.fixture(root, collision=True)
            output = root / "output"
            report = PACKER.pack(self.args(mod, output, assets))
            renamed = next(name for name in report.imported_files if name.startswith("shared__"))
            model = (output / "Files/model.fxsxml").read_text(encoding="utf-8")
            self.assertIn(renamed, model)
            self.assertTrue((output / "Files/shared.dds").is_file())
            self.assertTrue((output / "Files" / renamed).is_file())

    def test_packages_civ5_maps_and_map_scripts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mod = root / "Map Mod"
            mod.mkdir()
            (mod / "World.Civ5Map").write_bytes(b"map")
            (mod / "World.lua").write_text("function GenerateMap() end", encoding="utf-8")
            self.write_manifest(
                mod,
                "<File import='0'>World.Civ5Map</File><File import='0'>World.lua</File>",
                entries="<EntryPoint type='MapScript' file='World.lua' />",
            )
            assets = root / "Assets"
            output = root / "output"
            report = PACKER.pack(self.args(mod, output, assets))
            self.assertEqual(report.maps, ["World.Civ5Map", "World.lua"])
            package = ET.parse(next(output.glob("*.Civ5Pkg"))).getroot()
            self.assertEqual(package.findtext("./Gameplay/MapDirectory"), "Maps")

    def test_rejects_empty_mod_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mod = root / "Empty Mod"
            mod.mkdir()
            self.write_manifest(mod)
            with self.assertRaisesRegex(PACKER.PackError, "no DLC payload"):
                PACKER.pack(self.args(mod, root / "output", root / "Assets"))

    def test_rejects_xml_ingame_ui_context_instead_of_generating_invalid_lua_include(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mod = root / "UI Mod"
            mod.mkdir()
            (mod / "Screen.xml").write_text("<Context />", encoding="utf-8")
            self.write_manifest(
                mod,
                "<File import='0'>Screen.xml</File>",
                entries="<EntryPoint type='InGameUIAddin' file='Screen.xml' />",
            )
            with self.assertRaisesRegex(PACKER.PackError, "XML-based InGameUIAddin"):
                PACKER.pack(self.args(mod, root / "output", root / "Assets"))

    def test_corporations_lua_rewrite_resolves_shared_field_names_by_record_context(self):
        source = b"""local a = building.CorporationSpreadPressureModifier
local b = trait.CorporationSpreadPressureModifier
local c = belief.CorporationSpreadPressureModifier
local d = building.CorporationSpreadDistanceModifier
local e = era.CorporationSpreadDistanceModifier
"""
        result = PACKER.patch_corporations_lua(Path("Lua/Corp_Utils.lua"), source).decode()
        self.assertIn('CorpGetBuildingSetting(building, "CorporationSpreadPressureModifier")', result)
        self.assertIn('CorpGetTraitSetting(trait, "CorporationSpreadPressureModifier")', result)
        self.assertIn('CorpGetBeliefSetting(belief, "CorporationSpreadPressureModifier")', result)
        self.assertIn('CorpGetBuildingSetting(building, "CorporationSpreadDistanceModifier")', result)
        self.assertIn('CorpGetEraSetting(era, "CorporationSpreadDistanceModifier")', result)

    def test_corporations_xml_moves_core_fields_to_sidecar_rows(self):
        table = ET.fromstring(
            """<Buildings><Row><Type>BUILDING_TEST</Type><Cost>10</Cost>
            <CorporationOwnershipRequired>true</CorporationOwnershipRequired></Row></Buildings>"""
        )
        settings = {}
        PACKER.collect_corporation_settings(table, "Buildings", settings)
        self.assertIsNone(table.find("./Row/CorporationOwnershipRequired"))
        self.assertEqual(table.findtext("./Row/Cost"), "10")
        self.assertEqual(
            settings["CorporationBuildingSettings"]["BUILDING_TEST"]["CorporationOwnershipRequired"],
            "true",
        )
        compat = PACKER.corporations_compat_xml(settings)
        definitions = {node.get("name") for node in compat.findall("Table")}
        self.assertIn("CorporationBuildingSettings", definitions)
        building_types = [row.findtext("BuildingType") for row in compat.findall("./CorporationBuildingSettings/Row")]
        self.assertIn("BUILDING_TEST", building_types)


if __name__ == "__main__":
    unittest.main()
