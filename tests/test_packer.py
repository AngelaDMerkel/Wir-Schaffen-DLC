#!/usr/bin/env python3

import argparse
import json
import struct
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import civ5_dlc_packer as PACKER


class PackerTests(unittest.TestCase):
    def write_map(
        self,
        path: Path,
        width: int = 6,
        height: int = 4,
        world_size: str = "WORLDSIZE_COLOSSAL",
        players: int = 8,
    ) -> bytes:
        blocks = (
            b"TERRAIN_GRASS\0",
            b"FEATURE_FOREST\0",
            b"",
            b"RESOURCE_WHEAT\0",
            b"",
            b"Test Colossal Map\0",
            b"A test map\0",
        )
        header = bytes((12,)) + struct.pack("<II", width, height) + bytes((players,))
        header += struct.pack("<I", 0) + struct.pack("<7I", *(len(block) for block in blocks))
        world = world_size.encode("ascii")
        plot = bytes((0, 0xFF, 0xFF, 0, 0, 0xFF, 0, 0))
        data = header + b"".join(blocks) + struct.pack("<I", len(world)) + world + plot * (width * height)
        path.write_bytes(data)
        return data

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
            self.assertEqual(package_root.findtext("./GameData"), report.game_data[0])
            self.assertEqual(package_root.findtext("./TextData"), report.text_data[0])
            self.assertIsNone(package_root.find("./Gameplay/GameData"))
            self.assertIsNone(package_root.find("./Gameplay/TextData"))
            self.assertEqual(
                package_root.findtext("./Key"),
                PACKER.package_key_from_xml(package_root),
            )
            self.assertEqual(json.loads((output / "pack-report.json").read_text())["source_mod"], "Example")

    def test_package_key_matches_firaxis_upgrade_manifest(self):
        package = ET.fromstring(
            """<Civ5Package>
              <GUID>{8871E748-29A4-4910-8C57-8C99E32D0167}</GUID>
              <SteamApp>99999</SteamApp><Version>1</Version><Ownership>FREE</Ownership>
              <Key>e818fa28902977b42ee5e3426f5112e6</Key>
              <PTags><Tag>Version</Tag><Tag>Ownership</Tag></PTags>
            </Civ5Package>"""
        )
        self.assertEqual(
            PACKER.package_key_from_xml(package),
            "e818fa28902977b42ee5e3426f5112e6",
        )

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

    def test_inspects_a_standalone_custom_world_map(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "colossal.Civ5Map"
            data = self.write_map(path, width=300, height=10, world_size="WORLDSIZE_COLOSSAL", players=22)
            info = PACKER.inspect_civ5_map(path)
            self.assertEqual((info.width, info.height, info.plot_count), (300, 10, 3000))
            self.assertEqual(info.world_size, "WORLDSIZE_COLOSSAL")
            self.assertEqual(info.player_count, 22)
            self.assertEqual(info.sha256, __import__("hashlib").sha256(data).hexdigest())

    def test_packages_standalone_map_without_resizing_and_registers_world(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "extreme.Civ5Map"
            original = self.write_map(source, width=10, height=300, world_size="WORLDSIZE_EXTREME")
            output = root / "output"
            args = self.args(source, output, root / "Assets")
            args.map_file = True
            report = PACKER.pack(args)
            self.assertEqual((output / "Maps/extreme.Civ5Map").read_bytes(), original)
            world = ET.parse(output / report.game_data[0]).getroot().find("./Worlds/Replace")
            self.assertEqual(world.findtext("Type"), "WORLDSIZE_EXTREME")
            self.assertEqual(world.findtext("GridWidth"), "10")
            self.assertEqual(world.findtext("GridHeight"), "300")
            package = ET.parse(next(output.glob("*.Civ5Pkg"))).getroot()
            self.assertEqual(package.findtext("./GameData"), report.game_data[0])
            self.assertEqual(package.findtext("./TextData"), report.text_data[0])
            self.assertIn("WORLDSIZE_EXTREME", report.game_data[0])
            self.assertIn("WORLDSIZE_EXTREME", report.text_data[0])
            self.assertIsNone(package.find("./Gameplay/GameData"))
            self.assertEqual(package.findtext("./Gameplay/MapDirectory"), "Maps")
            metadata = json.loads((output / "pack-report.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["map"]["plot_count"], 3000)
            self.assertEqual(metadata["source_kind"], "Civ5Map")

    def test_world_compatibility_metadata_accepts_excogitare_recommendations(self):
        root = PACKER.world_compatibility_xml(
            "WORLDSIZE_EXTREME",
            180,
            94,
            20,
            None,
            default_minor_civs=20,
        )
        row = root.find("./Worlds/Replace")
        self.assertIsNotNone(row)
        self.assertEqual(row.findtext("Type"), "WORLDSIZE_EXTREME")
        self.assertEqual(row.findtext("GridWidth"), "180")
        self.assertEqual(row.findtext("GridHeight"), "94")
        self.assertEqual(row.findtext("DefaultPlayers"), "20")
        self.assertEqual(row.findtext("DefaultMinorCivs"), "20")
        text = PACKER.world_compatibility_text_xml("WORLDSIZE_EXTREME", 180, 94)
        self.assertIn("Extreme (180x94)", ET.tostring(text, encoding="unicode"))

    def test_rejects_map_over_the_file_format_plot_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "too-large.Civ5Map"
            self.write_map(path, width=400, height=100)
            with self.assertRaisesRegex(PACKER.PackError, "32,768"):
                PACKER.inspect_civ5_map(path)

    def test_rejects_empty_mod_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mod = root / "Empty Mod"
            mod.mkdir()
            self.write_manifest(mod)
            with self.assertRaisesRegex(PACKER.PackError, "no DLC payload"):
                PACKER.pack(self.args(mod, root / "output", root / "Assets"))

    def test_translates_legacy_promotion_prerequisites_to_the_stock_relation(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "promotions.xml"
            source.write_text(
                "<GameData><UnitPromotions><Row><Type>PROMOTION_TEST</Type>"
                "<PromotionPrereqOr1>PROMOTION_FIRST</PromotionPrereqOr1>"
                "<PromotionPrereqOr2>PROMOTION_SECOND</PromotionPrereqOr2>"
                "</Row></UnitPromotions></GameData>",
                encoding="utf-8",
            )
            game, text = PACKER.split_database_xml(source, future_worlds=False)
            self.assertIsNone(text)
            row = game.find("./UnitPromotions/Row")
            self.assertIsNone(row.find("PromotionPrereqOr1"))
            relations = game.findall("./UnitPromotions_PromotionPrereqOrs/Row")
            self.assertEqual(
                [item.findtext("PrerequisitePromotion") for item in relations],
                ["PROMOTION_FIRST", "PROMOTION_SECOND"],
            )
            self.assertTrue(all(item.findtext("PromotionType") == "PROMOTION_TEST" for item in relations))

    def test_translates_unimproved_feature_yields_to_the_bnw_table(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "trait.xml"
            source.write_text(
                "<GameData><Trait_UnimprovedFeatureYieldChanges><Row>"
                "<TraitType>TRAIT_TEST</TraitType><FeatureType>FEATURE_JUNGLE</FeatureType>"
                "<YieldType>YIELD_SCIENCE</YieldType><Yield>1</Yield>"
                "</Row></Trait_UnimprovedFeatureYieldChanges></GameData>",
                encoding="utf-8",
            )
            game, _ = PACKER.split_database_xml(source, future_worlds=False)
            row = game.find("./Trait_FeatureYieldChanges/Row")
            self.assertIsNotNone(row)
            self.assertEqual(row.findtext("AllowImprovement"), "false")
            self.assertIsNone(game.find("./Trait_UnimprovedFeatureYieldChanges"))

    def test_repairs_a_missing_xml_closing_tag_bracket(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "broken.xml"
            source.write_text(
                "<GameData><Units><Row><Type>UNIT_TEST</Type>\n"
                "<Pillage>true</Pillage\n<Moves>2</Moves></Row></Units></GameData>",
                encoding="utf-8",
            )
            root, ampersands, close_brackets = PACKER.parse_database_xml(source)
            self.assertEqual(ampersands, 0)
            self.assertEqual(close_brackets, 1)
            self.assertEqual(root.findtext("./Units/Row/Pillage"), "true")

    def test_mass_effect_sql_and_lua_are_translated_without_core_schema_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            mod = Path(temp)
            folder = mod / "Dummy Building Folder"
            folder.mkdir()
            tables = folder / "Mass Effect Dummy Building Tables.sql"
            visibility = folder / "Mass Effect Dummy Building IsVisible Set.sql"
            tables.write_text(
                "ALTER TABLE Buildings ADD IsVisible boolean DEFAULT 1;\n"
                "ALTER TABLE Buildings ADD PediaVisible boolean DEFAULT 1;\n",
                encoding="utf-8",
            )
            visibility.write_text(
                "UPDATE Buildings SET IsVisible = 0 WHERE Type = 'BUILDING_DUMMY';\n"
                "UPDATE Buildings SET PediaVisible = 0 WHERE Type = 'BUILDING_DUMMY';\n",
                encoding="utf-8",
            )
            hidden = PACKER.mass_effect_hidden_buildings(
                mod, sorted(PACKER.MASS_EFFECT_SQL_PATHS, key=str)
            )
            self.assertEqual(hidden, ("BUILDING_DUMMY",))
            city_view = PACKER.patch_mass_effect_lua(
                PurePosixPath("Dummy Building Folder/CityView.lua"),
                b"if (pCity:IsHasBuilding(buildingID)) and building.IsVisible then\nend\n",
                hidden,
            ).decode("utf-8")
            self.assertNotIn("building.IsVisible", city_view)
            self.assertIn("WirSchaffenMassEffectBuildingVisible", city_view)

            select = "SELECT Buildings.ID, Buildings.Description, Buildings.PortraitIndex, Buildings.IconAtlas"
            civilopedia_source = (
                select + " from Buildings;\n"
                + select
                + ", Buildings.PediaVisible -- custom\nFROM Buildings WHERE Buildings.PediaVisible = 1;\n"
                + select
                + ", Buildings.PediaVisible -- custom\nFROM Buildings WHERE Buildings.PediaVisible = 1;\n"
                "\t\tfor building in BuildingsByEra(era.Type) do\n"
                "\t\t\tAddArticle(eraID, tableid, building);\n"
                "\t\t\ttableid = tableid + 1;\n"
                "\t\tend\n"
                "\t\t\tfor building in DB.Query(sql) do\n"
                "\t\t\t\tAddArticle(eraID, tableid, building);\n"
                "\t\t\t\ttableid = tableid + 1;\n"
                "\t\t\tend\n"
            )
            civilopedia = PACKER.patch_mass_effect_lua(
                PurePosixPath("Dummy Building Folder/CivilopediaScreen.lua"),
                civilopedia_source.encode("utf-8"),
                hidden,
            ).decode("utf-8")
            self.assertNotIn("PediaVisible", civilopedia)
            self.assertIn("Buildings.Type", civilopedia)
            self.assertEqual(civilopedia.count("WirSchaffenMassEffectBuildingVisible(building)"), 3)

    def test_repairs_bare_ampersands_in_source_database_and_imported_xml(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mod = root / "Malformed XML Mod"
            mod.mkdir()
            (mod / "text.xml").write_text(
                '<GameData><Language_en_US><Row Tag="TXT_TEST"><Text>Gods & Kings</Text>'
                '</Row></Language_en_US></GameData>',
                encoding="utf-8",
            )
            (mod / "screen.xml").write_text('<Context Label="A & B" />', encoding="utf-8")
            self.write_manifest(
                mod,
                "<File import='0'>text.xml</File><File import='1'>screen.xml</File>",
                "<UpdateDatabase>text.xml</UpdateDatabase>",
            )
            output = root / "output"
            report = PACKER.pack(self.args(mod, output, root / "Assets"))
            self.assertTrue(any("bare ampersand" in warning for warning in report.warnings))
            self.assertEqual(
                ET.parse(output / report.text_data[0]).getroot().findtext("./Language_en_US/Row/Text"),
                "Gods & Kings",
            )
            self.assertIn('A &amp; B', (output / "Files/screen.xml").read_text(encoding="utf-8"))

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
        self.assertTrue(result.startswith('include("Corp_DLCCompat.lua");'))

    def test_corporations_compatibility_settings_are_embedded_in_lua(self):
        settings = {}
        PACKER.seed_corporation_sql_settings(settings)
        result = PACKER.corporations_compat_lua(settings)
        self.assertIn('["WORLDSIZE_HUGE"]', result)
        self.assertIn('CorporationSpreadDistance = 24', result)
        self.assertIn('["WORLDSIZE_COLOSSAL"]', result)
        self.assertIn('["WORLDSIZE_EXTREME"]', result)
        self.assertIn('["GAMESPEED_STANDARD"]', result)
        self.assertIn('CorporationPressureNeededToCreateFan = 1500', result)
        self.assertNotIn("GameInfo.CorporationWorldSettings", result)

    def test_future_worlds_resource_lookup_is_nil_safe(self):
        source = b"""if tResouceValidImprovementsCityNearbyMapDatas[iResourceID][iImprovementID] then end
local bResourceIsCorrectlyImproved = false
if not bResourceIsArtifact then
    bResourceIsCorrectlyImproved = (tResouceValidImprovementsCityNearbyMapDatas[iResourceID][iImprovementID] ~= nil)
end
"""
        result = PACKER.patch_future_worlds_city_nearby_data(source).decode()
        self.assertIn("or {}", result)
        self.assertNotIn(
            "tResouceValidImprovementsCityNearbyMapDatas[iResourceID][iImprovementID]",
            result,
        )

    def test_really_advanced_setup_uses_manifest_version_outside_mods_context(self):
        source = (
            b'g_UserData = Modding.OpenUserData("GTAS_AdvancedSetupMod", '
            b'Modding.GetActivatedModVersion(MOD_ID));\n'
        )
        result = PACKER.patch_really_advanced_setup_lua(
            Path("DataManager/GTAS_DataManager.lua"), source, 15
        ).decode()
        self.assertIn('Modding.OpenUserData("GTAS_AdvancedSetupMod", 15)', result)
        self.assertNotIn("GetActivatedModVersion", result)

    def test_new_unit_rows_receive_a_help_fallback(self):
        table = ET.fromstring(
            """<Units><Row><Type>UNIT_TEST</Type><Description>TXT_KEY_UNIT_TEST</Description>
            <Strategy>TXT_KEY_UNIT_TEST_STRATEGY</Strategy></Row></Units>"""
        )
        count = PACKER.add_missing_unit_help(table)
        self.assertEqual(count, 1)
        self.assertEqual(table.findtext("./Row/Help"), "TXT_KEY_UNIT_TEST_STRATEGY")

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
