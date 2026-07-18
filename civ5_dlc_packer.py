#!/usr/bin/env python3
"""Pack a Civilization V ModBuddy mod as a DLC directory.

The normal Civ V mod loader accepts SQL and InGameUIAddin entry points, while
the DLC package loader does not.  This tool handles the common XML/import case
and contains explicit compatibility compilers for Future Worlds v6 and
Corporations (Brave New World) v1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


FUTURE_WORLDS_ID = "d9ece224-6cd8-4519-a27a-c417b59cdf35"
CORPORATIONS_ID = "199bbbf8-f80e-449e-a67e-036a7248fb13"
PACKER_NAMESPACE = uuid.UUID("07186f1e-bfdd-45db-8c1d-f9679e727249")
SUPPORTED_ACTION = "UpdateDatabase"

CORPORATION_SIDECARS = {
    "Buildings": (
        "CorporationBuildingSettings",
        "BuildingType",
        {
            "CorporationOwnershipRequired": ("boolean", 0),
            "CorporationHeadquartersCity": ("boolean", 0),
            "CorporationSpreadPressureModifier": ("integer", 0),
            "CorporationSpreadDistanceModifier": ("integer", 0),
            "CorporationFranchiseGoldRevenueModifier": ("integer", 0),
        },
    ),
    "Worlds": (
        "CorporationWorldSettings",
        "WorldType",
        {"CorporationSpreadDistance": ("integer", 18)},
    ),
    "Eras": (
        "CorporationEraSettings",
        "EraType",
        {"CorporationSpreadDistanceModifier": ("integer", 0)},
    ),
    "GameSpeeds": (
        "CorporationGameSpeedSettings",
        "GameSpeedType",
        {"CorporationPressureNeededToCreateFan": ("integer", 2000)},
    ),
    "Technologies": (
        "CorporationTechnologySettings",
        "TechnologyType",
        {
            "AllowsCorporationSpread": ("boolean", 0),
            "AllowsUnlimitedCorporationSpreadDistance": ("boolean", 0),
        },
    ),
    "Policies": (
        "CorporationPolicySettings",
        "PolicyType",
        {
            "CorporationLocalFranchiseGoldRevenueModifier": ("integer", 0),
            "CorporationForeignFranchiseGoldRevenueModifier": ("integer", 0),
            "SharedIdeologyCorporationSpreadPressureModifier": ("integer", 0),
            "DifferentIdeologyCorporationSpreadPressureModifier": ("integer", 0),
        },
    ),
    "Traits": (
        "CorporationTraitSettings",
        "TraitType",
        {"CorporationSpreadPressureModifier": ("integer", 0)},
    ),
    "Beliefs": (
        "CorporationBeliefSettings",
        "BeliefType",
        {"CorporationSpreadPressureModifier": ("integer", 0)},
    ),
}

CORPORATION_FIELDS = {
    field
    for table_name, (table, key, fields) in CORPORATION_SIDECARS.items()
    for field in fields
}
CORPORATION_FIELD_TABLES = {
    field: tuple(
        (
            source_table,
            f"CorpGet{sidecar.removeprefix('Corporation').removesuffix('Settings')}Setting",
        )
        for source_table, (sidecar, key, fields) in CORPORATION_SIDECARS.items()
        if field in fields
    )
    for field in CORPORATION_FIELDS
}


class PackError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModFile:
    path: PurePosixPath
    md5: str
    imported: bool


@dataclass(frozen=True)
class EntryPoint:
    kind: str
    path: PurePosixPath


@dataclass(frozen=True)
class Manifest:
    path: Path
    mod_id: str
    version: int
    name: str
    description: str
    supports_multiplayer: bool | None
    supports_mac: bool | None
    dependencies: tuple[str, ...]
    references: tuple[str, ...]
    files: tuple[ModFile, ...]
    actions: tuple[tuple[str, PurePosixPath], ...]
    entry_points: tuple[EntryPoint, ...]


@dataclass
class BuildReport:
    source_mod: str
    source_id: str
    source_version: int
    dlc_id: str
    ui_set: str
    game_data: list[str]
    text_data: list[str]
    imported_files: list[str]
    maps: list[str]
    entry_points: list[str]
    translated_sql: list[str]
    omitted_features: list[str]
    warnings: list[str]
    validation: list[str]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_manifest(mod_dir: Path) -> Manifest:
    manifests = sorted(mod_dir.glob("*.modinfo"))
    if len(manifests) != 1:
        raise PackError(f"expected exactly one .modinfo in {mod_dir}, found {len(manifests)}")
    path = manifests[0]
    root = ET.parse(path).getroot()
    if local_name(root.tag) != "Mod":
        raise PackError(f"{path} is not a ModBuddy manifest")

    props = root.find("Properties")
    name = props.findtext("Name") if props is not None else None
    description = props.findtext("Description") if props is not None else None
    def optional_bool(name: str) -> bool | None:
        value = props.findtext(name) if props is not None else None
        return None if value is None else value.strip() == "1"

    def mod_links(section: str) -> tuple[str, ...]:
        values = []
        for node in root.findall(f"./{section}/Mod"):
            values.append(node.get("title") or node.get("id") or "unknown mod")
        return tuple(values)

    files: list[ModFile] = []
    files_node = root.find("Files")
    if files_node is not None:
        for node in files_node.findall("File"):
            if not node.text:
                continue
            files.append(
                ModFile(
                    PurePosixPath(node.text.strip()),
                    node.get("md5", "").upper(),
                    node.get("import", "0") == "1",
                )
            )

    actions: list[tuple[str, PurePosixPath]] = []
    action_parent = root.find("./Actions/OnModActivated")
    if action_parent is not None:
        for node in action_parent:
            if node.text and node.text.strip():
                actions.append((local_name(node.tag), PurePosixPath(node.text.strip())))

    entry_points: list[EntryPoint] = []
    entry_parent = root.find("EntryPoints")
    if entry_parent is not None:
        for node in entry_parent.findall("EntryPoint"):
            if node.get("file"):
                entry_points.append(EntryPoint(node.get("type", ""), PurePosixPath(node.get("file"))))

    try:
        version = int(root.get("version", "0"))
        mod_id = str(uuid.UUID(root.get("id", "")))
    except (ValueError, TypeError) as exc:
        raise PackError(f"invalid mod UUID/version in {path}: {exc}") from exc
    return Manifest(
        path=path,
        mod_id=mod_id,
        version=version,
        name=name or mod_dir.name,
        description=description or name or mod_dir.name,
        supports_multiplayer=optional_bool("SupportsMultiplayer"),
        supports_mac=optional_bool("SupportsMac"),
        dependencies=mod_links("Dependencies"),
        references=mod_links("References"),
        files=tuple(files),
        actions=tuple(actions),
        entry_points=tuple(entry_points),
    )


def resolve_mod_file(mod_dir: Path, rel_path: PurePosixPath) -> Path:
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise PackError(f"unsafe path in mod manifest: {rel_path}")
    path = mod_dir.joinpath(*rel_path.parts)
    if not path.is_file():
        raise PackError(f"manifest refers to missing file: {rel_path}")
    return path


def validate_files(mod_dir: Path, manifest: Manifest) -> None:
    errors: list[str] = []
    for item in manifest.files:
        path = resolve_mod_file(mod_dir, item.path)
        if item.md5:
            actual = hashlib.md5(path.read_bytes()).hexdigest().upper()
            if actual != item.md5:
                errors.append(f"{item.path}: expected {item.md5}, got {actual}")
    if errors:
        raise PackError("mod file validation failed:\n  " + "\n  ".join(errors))


def slug(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return clean or "Mod"


def add_text(parent: ET.Element, tag: str, value: object) -> None:
    child = ET.SubElement(parent, tag)
    if isinstance(value, bytes):
        child.text = value.hex()
    elif isinstance(value, float):
        child.text = format(value, ".15g")
    else:
        child.text = str(value)


def add_row(table: ET.Element, values: dict[str, object], columns: Sequence[str] | None = None) -> None:
    row = ET.SubElement(table, "Row")
    selected = columns if columns is not None else values.keys()
    for key in selected:
        value = values.get(key)
        if value is not None:
            add_text(row, key, value)


def add_update(table: ET.Element, where: dict[str, object], values: dict[str, object]) -> None:
    update = ET.SubElement(table, "Update")
    ET.SubElement(update, "Where", {key: str(value) for key, value in where.items()})
    ET.SubElement(update, "Set", {key: str(value) for key, value in values.items()})


def write_xml(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    tree.write(path, encoding="utf-8", xml_declaration=True, short_empty_elements=True)


def child_value(node: ET.Element, name: str) -> str | None:
    if name in node.attrib:
        return node.attrib[name]
    child = node.find(name)
    return child.text if child is not None else None


def collect_corporation_settings(
    table: ET.Element,
    table_name: str,
    settings: dict[str, dict[str, dict[str, str]]],
) -> None:
    sidecar_name, key_name, fields = CORPORATION_SIDECARS[table_name]
    for action in list(table):
        kind = local_name(action.tag) if isinstance(action.tag, str) else ""
        if kind == "Row":
            type_name = child_value(action, "Type")
            value_node = action
        elif kind == "Update":
            where = action.find("Where")
            value_node = action.find("Set")
            type_name = child_value(where, "Type") if where is not None else None
        else:
            continue
        if value_node is None:
            continue
        found: dict[str, str] = {}
        for field in fields:
            if field in value_node.attrib:
                found[field] = value_node.attrib.pop(field)
            for child in list(value_node):
                if local_name(child.tag) == field:
                    found[field] = child.text or ""
                    value_node.remove(child)
        if found:
            if not type_name:
                raise PackError(f"Corporations compatibility requires a Type key for {table_name} {kind}")
            settings.setdefault(sidecar_name, {}).setdefault(type_name, {}).update(found)
        if kind == "Update" and not value_node.attrib and not list(value_node):
            table.remove(action)


def split_database_xml(
    source: Path,
    future_worlds: bool,
    corporation_settings: dict[str, dict[str, dict[str, str]]] | None = None,
) -> tuple[ET.Element | None, ET.Element | None]:
    try:
        root = ET.parse(source).getroot()
    except ET.ParseError as exc:
        raise PackError(f"invalid database XML {source}: {exc}") from exc
    if local_name(root.tag) != "GameData":
        raise PackError(f"database XML must have a GameData root: {source}")
    game_root = ET.Element("GameData")
    text_root = ET.Element("GameData")
    for table in list(root):
        if not isinstance(table.tag, str):
            continue
        table_name = local_name(table.tag)
        if future_worlds and table_name == "Eras":
            for splash in list(table.iter("SplashScreen")):
                parent = next((p for p in table.iter() if splash in list(p)), None)
                if parent is not None:
                    parent.remove(splash)
        if corporation_settings is not None and table_name in CORPORATION_SIDECARS:
            collect_corporation_settings(table, table_name, corporation_settings)
        target = text_root if table_name.startswith("Language_") else game_root
        target.append(table)
    return (game_root if len(game_root) else None, text_root if len(text_root) else None)


def copy_database_xml(
    source: Path,
    output: Path,
    index: int,
    original_name: str,
    future_worlds: bool,
    corporation_settings: dict[str, dict[str, dict[str, str]]] | None = None,
) -> tuple[list[str], list[str]]:
    game_root, text_root = split_database_xml(source, future_worlds, corporation_settings)
    stem = slug(Path(original_name).stem)
    game_files: list[str] = []
    text_files: list[str] = []
    if game_root is not None:
        name = f"{index:03d}_{stem}_GameData.xml"
        write_xml(output / name, game_root)
        game_files.append(name)
    if text_root is not None:
        name = f"{index:03d}_{stem}_TextData.xml"
        write_xml(output / name, text_root)
        text_files.append(name)
    return game_files, text_files


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> dict[str, object]:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise PackError(f"base database is missing a row needed by Future Worlds: {params or sql}")
    return dict(row)


def copy_source_row(
    conn: sqlite3.Connection,
    root: ET.Element,
    table_name: str,
    key_name: str,
    source_key: str,
    replacements: dict[str, object],
    columns: Sequence[str],
) -> None:
    row = query_one(conn, f'SELECT * FROM "{table_name}" WHERE "{key_name}"=?', (source_key,))
    row.update(replacements)
    table = ET.SubElement(root, table_name)
    add_row(table, row, columns)


def future_worlds_compat_xml(base_db: Path, mod_dir: Path) -> ET.Element:
    if not base_db.is_file():
        raise PackError(f"base database not found: {base_db}")
    conn = sqlite3.connect(f"file:{base_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    root = ET.Element("GameData")
    try:
        copy_source_row(
            conn,
            root,
            "ArtDefine_UnitInfos",
            "Type",
            "ART_DEF_UNIT_U_AZTEC_JAGUAR",
            {"Type": "ART_DEF_UNIT_FW_MUTANT", "Formation": "Barbarian"},
            ("Type", "DamageStates", "Formation"),
        )
        members = ET.SubElement(root, "ArtDefine_UnitInfoMemberInfos")
        add_row(
            members,
            {
                "UnitInfoType": "ART_DEF_UNIT_FW_MUTANT",
                "UnitMemberInfoType": "ART_DEF_UNIT_MEMBER_FW_MUTANT",
                "NumMembers": 6,
            },
        )
        copy_source_row(
            conn,
            root,
            "ArtDefine_UnitMemberCombats",
            "UnitMemberType",
            "ART_DEF_UNIT_MEMBER_U_AZTEC_JAGUAR",
            {"UnitMemberType": "ART_DEF_UNIT_MEMBER_FW_MUTANT"},
            (
                "UnitMemberType", "EnableActions", "DisableActions", "MoveRadius", "ShortMoveRadius",
                "ChargeRadius", "AttackRadius", "RangedAttackRadius", "MoveRate", "ShortMoveRate",
                "TurnRateMin", "TurnRateMax", "TurnFacingRateMin", "TurnFacingRateMax", "RollRateMin",
                "RollRateMax", "PitchRateMin", "PitchRateMax", "LOSRadiusScale", "TargetRadius",
                "TargetHeight", "HasShortRangedAttack", "HasLongRangedAttack", "HasLeftRightAttack",
                "HasStationaryMelee", "HasStationaryRangedAttack", "HasRefaceAfterCombat",
                "ReformBeforeCombat", "HasIndependentWeaponFacing", "HasOpponentTracking",
                "HasCollisionAttack", "AttackAltitude", "AltitudeDecelerationDistance",
                "OnlyTurnInMovementActions", "RushAttackFormation",
            ),
        )
        weapon_rows = conn.execute(
            'SELECT * FROM ArtDefine_UnitMemberCombatWeapons WHERE UnitMemberType=?',
            ("ART_DEF_UNIT_MEMBER_U_AZTEC_JAGUAR",),
        ).fetchall()
        weapons = ET.SubElement(root, "ArtDefine_UnitMemberCombatWeapons")
        weapon_cols = (
            "UnitMemberType", "Index", "SubIndex", "ID", "VisKillStrengthMin", "VisKillStrengthMax",
            "ProjectileSpeed", "ProjectileTurnRateMin", "ProjectileTurnRateMax", "HitEffect",
            "HitEffectScale", "HitRadius", "ProjectileChildEffectScale", "AreaDamageDelay",
            "ContinuousFire", "WaitForEffectCompletion", "TargetGround", "IsDropped", "WeaponTypeTag",
            "WeaponTypeSoundOverrideTag",
        )
        for source_row in weapon_rows:
            row = dict(source_row)
            row["UnitMemberType"] = "ART_DEF_UNIT_MEMBER_FW_MUTANT"
            add_row(weapons, row, weapon_cols)
        copy_source_row(
            conn,
            root,
            "ArtDefine_UnitMemberInfos",
            "Type",
            "ART_DEF_UNIT_MEMBER_U_AZTEC_JAGUAR",
            {"Type": "ART_DEF_UNIT_MEMBER_FW_MUTANT", "Scale": 0.16, "Model": "zombie.fxsxml"},
            ("Type", "Scale", "ZOffset", "Domain", "Model", "MaterialTypeTag", "MaterialTypeSoundOverrideTag"),
        )

        resource_types = ET.SubElement(root, "Improvement_ResourceTypes")
        seen_resources: set[str] = set()
        for source_row in conn.execute(
            "SELECT ResourceType FROM Improvement_ResourceTypes "
            "WHERE ImprovementType IN ('IMPROVEMENT_FARM','IMPROVEMENT_PLANTATION') ORDER BY ResourceType"
        ):
            resource = source_row[0]
            if resource not in seen_resources:
                add_row(resource_types, {"ImprovementType": "IMPROVEMENT_FW_FUNGAL_GROWTH", "ResourceType": resource})
                seen_resources.add(resource)
        resource_yields = ET.SubElement(root, "Improvement_ResourceType_Yields")
        seen_yields: set[tuple[object, ...]] = set()
        for source_row in conn.execute(
            "SELECT ResourceType,YieldType,Yield FROM Improvement_ResourceType_Yields "
            "WHERE ImprovementType IN ('IMPROVEMENT_FARM','IMPROVEMENT_PLANTATION') "
            "ORDER BY ResourceType,YieldType,Yield"
        ):
            key = tuple(source_row)
            if key not in seen_yields:
                add_row(
                    resource_yields,
                    {
                        "ImprovementType": "IMPROVEMENT_FW_FUNGAL_GROWTH",
                        "ResourceType": source_row[0],
                        "YieldType": source_row[1],
                        "Yield": source_row[2],
                    },
                )
                seen_yields.add(key)

        base_future = query_one(conn, "SELECT GridX FROM Technologies WHERE Type='TECH_FUTURE_TECH'")["GridX"]
        tech_source = ET.parse(mod_dir / "XML/FutureTechnologies.xml").getroot()
        tech_values: dict[str, int] = {}
        for row in tech_source.findall("./Technologies/Row"):
            type_name = row.findtext("Type")
            grid_x = row.findtext("GridX")
            if type_name and grid_x is not None:
                tech_values[type_name] = int(grid_x) + int(base_future) - 17
        technologies = ET.SubElement(root, "Technologies")
        for type_name, grid_x in sorted(tech_values.items()):
            add_update(technologies, {"Type": type_name}, {"GridX": grid_x})
        base_max = conn.execute("SELECT MAX(GridX) FROM Technologies").fetchone()[0]
        add_update(
            technologies,
            {"Type": "TECH_FUTURE_TECH"},
            {"GridX": max([int(base_max), *tech_values.values()]) + 1},
        )

        eras = ET.SubElement(root, "Eras")
        add_update(eras, {"Type": "ERA_FW_FUTURE"}, {"ID": 99})
        add_update(eras, {"Type": "ERA_FUTURE"}, {"ID": 8})
        add_update(eras, {"Type": "ERA_FW_FUTURE"}, {"ID": 7})

        has_enlightenment = conn.execute("SELECT 1 FROM Units WHERE Type='UNIT_EE_SKIRMISHER'").fetchone()
        if has_enlightenment:
            buildings = ET.SubElement(root, "Buildings")
            add_update(
                buildings,
                {"Type": "BUILDING_FW_JURASSIC_PARK"},
                {"FreeBuildingThisCity": "BUILDINGCLASS_EE_MENAGERIE"},
            )
            changes = ET.SubElement(root, "Building_BuildingClassYieldChanges")
            add_update(
                changes,
                {"BuildingType": "BUILDING_FW_JURASSIC_PARK"},
                {"BuildingClassType": "BUILDINGCLASS_EE_MENAGERIE"},
            )
            happiness = ET.SubElement(root, "Building_BuildingClassHappiness")
            add_update(
                happiness,
                {"BuildingType": "BUILDING_FW_JURASSIC_PARK"},
                {"BuildingClassType": "BUILDINGCLASS_EE_MENAGERIE"},
            )
    finally:
        conn.close()
    return root


def seed_corporation_sql_settings(settings: dict[str, dict[str, dict[str, str]]]) -> None:
    values = {
        "CorporationBuildingSettings": {
            "BUILDING_BROADCAST_TOWER": {"CorporationSpreadDistanceModifier": "25"},
        },
        "CorporationWorldSettings": {
            "WORLDSIZE_DUEL": {"CorporationSpreadDistance": "12"},
            "WORLDSIZE_TINY": {"CorporationSpreadDistance": "12"},
            "WORLDSIZE_SMALL": {"CorporationSpreadDistance": "15"},
            "WORLDSIZE_STANDARD": {"CorporationSpreadDistance": "18"},
            "WORLDSIZE_LARGE": {"CorporationSpreadDistance": "21"},
            "WORLDSIZE_HUGE": {"CorporationSpreadDistance": "24"},
        },
        "CorporationEraSettings": {
            "ERA_MODERN": {"CorporationSpreadDistanceModifier": "33"},
            "ERA_POSTMODERN": {"CorporationSpreadDistanceModifier": "66"},
            "ERA_FUTURE": {"CorporationSpreadDistanceModifier": "100"},
        },
        "CorporationGameSpeedSettings": {
            "GAMESPEED_QUICK": {"CorporationPressureNeededToCreateFan": "1000"},
            "GAMESPEED_STANDARD": {"CorporationPressureNeededToCreateFan": "1500"},
            "GAMESPEED_EPIC": {"CorporationPressureNeededToCreateFan": "3000"},
            "GAMESPEED_MARATHON": {"CorporationPressureNeededToCreateFan": "6000"},
        },
        "CorporationTechnologySettings": {
            "TECH_GLOBALIZATION": {"AllowsUnlimitedCorporationSpreadDistance": "1"},
        },
        "CorporationPolicySettings": {
            "POLICY_COMMERCE": {"CorporationLocalFranchiseGoldRevenueModifier": "100"},
            "POLICY_PROTECTIONISM": {"CorporationForeignFranchiseGoldRevenueModifier": "100"},
        },
        "CorporationTraitSettings": {
            "TRAIT_RIVER_EXPANSION": {"CorporationSpreadPressureModifier": "100"},
        },
    }
    for table, rows in values.items():
        for type_name, fields in rows.items():
            settings.setdefault(table, {}).setdefault(type_name, {}).update(fields)


def corporations_compat_xml(settings: dict[str, dict[str, dict[str, str]]]) -> ET.Element:
    seed_corporation_sql_settings(settings)
    root = ET.Element("GameData")
    for source_table, (sidecar, key_name, fields) in CORPORATION_SIDECARS.items():
        definition = ET.SubElement(root, "Table", {"name": sidecar})
        ET.SubElement(
            definition,
            "Column",
            {"name": "ID", "type": "integer", "primarykey": "true", "autoincrement": "true"},
        )
        ET.SubElement(definition, "Column", {"name": key_name, "type": "text", "notnull": "true", "unique": "true"})
        for field, (field_type, default) in fields.items():
            ET.SubElement(
                definition,
                "Column",
                {"name": field, "type": field_type, "default": str(default)},
            )
        rows_node = ET.SubElement(root, sidecar)
        for type_name, values in sorted(settings.get(sidecar, {}).items()):
            add_row(rows_node, {key_name: type_name, **values})

    buildings = ET.SubElement(root, "Buildings")
    add_update(
        buildings,
        {"Type": "BUILDING_BROADCAST_TOWER"},
        {
            "Help": "TXT_KEY_BUILDING_BROADCAST_TOWER_CORP_HELP",
            "Strategy": "TXT_KEY_BUILDING_BROADCAST_TOWER_CORP_STRATEGY",
        },
    )
    traits = ET.SubElement(root, "Traits")
    add_update(
        traits,
        {"Type": "TRAIT_RIVER_EXPANSION"},
        {"Description": "TXT_KEY_TRAIT_RIVER_EXPANSION_CORP"},
    )
    return root


def corporations_compat_lua() -> str:
    blocks = [
        "-- Generated by civ5-dlc-packer.py for Corporations v1",
        "-- Moves configuration formerly stored in late ALTER TABLE columns into DLC-safe sidecar tables.",
        "",
        "local function CorpBuildSettingsLookup(infoTable, keyName)",
        "  local result = {}",
        "  if infoTable then",
        "    for row in infoTable() do",
        "      result[row[keyName]] = row",
        "    end",
        "  end",
        "  return result",
        "end",
        "",
    ]
    for source_table, (sidecar, key_name, fields) in CORPORATION_SIDECARS.items():
        short_name = sidecar.removeprefix("Corporation").removesuffix("Settings")
        variable = f"g_Corp{short_name}Settings"
        blocks.append(f'local {variable} = CorpBuildSettingsLookup(GameInfo.{sidecar}, "{key_name}")')
        blocks.append(f"function CorpGet{short_name}Setting(record, fieldName)")
        blocks.append("  if not record then return 0 end")
        blocks.append(f"  local setting = {variable}[record.Type]")
        blocks.append("  if setting and setting[fieldName] ~= nil then return setting[fieldName] end")
        blocks.append("  local defaults = {")
        for field, (field_type, default) in fields.items():
            blocks.append(f'    ["{field}"] = {default},')
        blocks.append("  }")
        blocks.append("  return defaults[fieldName] or 0")
        blocks.append("end")
        blocks.append("")
    return "\n".join(blocks) + "\n"


def patch_corporations_lua(path: PurePosixPath, data: bytes) -> bytes:
    text = data.decode("utf-8-sig")
    if path.as_posix() == "Lua/Corporations.lua":
        text = 'include("Corp_DLCCompat.lua");\n' + text
    variable_sources = {
        "building": "Buildings",
        "world": "Worlds",
        "era": "Eras",
        "gamespeed": "GameSpeeds",
        "tech": "Technologies",
        "massmediatech": "Technologies",
        "globaltech": "Technologies",
        "policy": "Policies",
        "trait": "Traits",
        "belief": "Beliefs",
    }
    for field, choices in CORPORATION_FIELD_TABLES.items():
        for source_table, helper in choices:
            direct = re.compile(
                rf"GameInfo\.{re.escape(source_table)}\[([^\]\n]+)\]\.{re.escape(field)}"
            )
            text = direct.sub(
                lambda match, source_table=source_table, helper=helper: (
                    f'{helper}(GameInfo.{source_table}[{match.group(1)}], "{field}")'
                ),
                text,
            )
        variable = re.compile(rf"\b([A-Za-z_][A-Za-z0-9_]*)\.{re.escape(field)}\b")
        def replace_variable(match: re.Match[str]) -> str:
            variable_name = match.group(1)
            if len(choices) == 1:
                helper = choices[0][1]
            else:
                source = variable_sources.get(variable_name.lower())
                matching = [helper for source_table, helper in choices if source_table == source]
                if len(matching) != 1:
                    return match.group(0)
                helper = matching[0]
            return f'{helper}({variable_name}, "{field}")'
        text = variable.sub(replace_variable, text)
    remaining = [field for field in CORPORATION_FIELDS if re.search(rf"\.{re.escape(field)}\b", text)]
    if remaining:
        raise PackError(f"untranslated Corporations fields in {path}: {', '.join(remaining)}")
    return text.encode("utf-8")


def patch_future_worlds_popup(data: bytes) -> bytes:
    text = data.decode("utf-8-sig")
    old = "    lastBackgroundImage = GameInfo.Eras[iEra].SplashScreen;"
    if old not in text:
        raise PackError("Future Worlds NewEraPopup.lua is not the expected v6 file")
    replacement = """    local eraSplashScreens = {
        ERA_CLASSICAL = \"ERA_Classical.dds\",
        ERA_MEDIEVAL = \"ERA_Medievel.dds\",
        ERA_RENAISSANCE = \"ERA_Renissance.dds\",
        ERA_INDUSTRIAL = \"ERA_Industrial.dds\",
        ERA_MODERN = \"ERA_Modern.dds\",
        ERA_POSTMODERN = \"ERA_Atomic.dds\",
        ERA_FUTURE = \"ERA_Future.dds\",
        ERA_FW_FUTURE = \"ERA_FW_Future.dds\"
    };
    lastBackgroundImage = eraSplashScreens[GameInfo.Eras[iEra].Type] or \"ERA_Medievel.dds\";"""
    return text.replace(old, replacement).encode("utf-8")


def resolve_ingame_script(mod_dir: Path, manifest: Manifest, entry: EntryPoint) -> PurePosixPath:
    if entry.path.suffix.lower() == ".lua":
        return entry.path
    if entry.path.suffix.lower() == ".xml":
        source = resolve_mod_file(mod_dir, entry.path)
        try:
            root_name = local_name(ET.parse(source).getroot().tag)
        except ET.ParseError as exc:
            raise PackError(f"invalid InGameUIAddin XML {entry.path}: {exc}") from exc
        companion = entry.path.with_suffix(".lua")
        manifest_paths = {item.path for item in manifest.files}
        if root_name == "GameData" and companion in manifest_paths:
            resolve_mod_file(mod_dir, companion)
            return companion
    raise PackError(f"XML-based InGameUIAddin contexts are not yet supported: {entry.path}")


def effective_ui_source(game_assets: Path, ui_set: str, relative: str) -> Path:
    if ui_set == "BaseGame":
        candidate = game_assets / "UI" / "InGame" / relative
    else:
        candidate = game_assets / "DLC" / ui_set / "UI" / "InGame" / relative
    if not candidate.is_file():
        raise PackError(f"could not find {ui_set} UI source: {candidate}")
    return candidate


def copy_imports(
    mod_dir: Path,
    manifest: Manifest,
    output: Path,
    future_worlds: bool,
    corporations: bool,
) -> list[str]:
    files_dir = output / "Files"
    files_dir.mkdir()
    selected = [item for item in manifest.files if item.imported]
    entry_paths = {
        resolve_ingame_script(mod_dir, manifest, entry)
        for entry in manifest.entry_points
        if entry.kind == "InGameUIAddin"
    }
    for entry_path in entry_paths:
        if not any(item.path == entry_path for item in selected):
            selected.append(ModFile(entry_path, "", True))

    source_data: dict[PurePosixPath, bytes] = {}
    for item in selected:
        source = resolve_mod_file(mod_dir, item.path)
        data = source.read_bytes()
        if future_worlds and item.path.as_posix() == "Lua/NewEraPopup.lua":
            data = patch_future_worlds_popup(data)
        if corporations and item.path.suffix.lower() == ".lua":
            data = patch_corporations_lua(item.path, data)
        source_data[item.path] = data

    groups: dict[str, list[PurePosixPath]] = {}
    for path in source_data:
        groups.setdefault(path.name.lower(), []).append(path)
    output_name: dict[PurePosixPath, str] = {}
    for paths in groups.values():
        paths.sort(key=lambda path: next(i for i, item in enumerate(selected) if item.path == path))
        unique_hashes: dict[str, str] = {}
        for index, path in enumerate(paths):
            digest = hashlib.sha256(source_data[path]).hexdigest()
            if digest in unique_hashes:
                output_name[path] = unique_hashes[digest]
            elif index == 0:
                output_name[path] = path.name
                unique_hashes[digest] = path.name
            else:
                suffixes = "".join(Path(path.name).suffixes)
                stem = path.name.removesuffix(suffixes) if suffixes else path.name
                renamed = f"{stem}__{digest[:8]}{suffixes}"
                output_name[path] = renamed
                unique_hashes[digest] = renamed

    text_extensions = {".xml", ".fxsxml", ".ftsxml", ".lua", ".txt", ".csv", ".json"}
    written: dict[str, bytes] = {}
    for path, raw_data in source_data.items():
        data = raw_data
        if path.suffix.lower() in text_extensions:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = ""
            if text:
                for sibling, sibling_name in output_name.items():
                    if sibling.parent == path.parent and sibling_name != sibling.name:
                        text = re.sub(re.escape(sibling.name), sibling_name, text, flags=re.IGNORECASE)
                data = text.encode("utf-8")
        name = output_name[path]
        existing = written.get(name.lower())
        if existing is not None and existing != data:
            raise PackError(f"internal output collision after renaming imported file: {name}")
        written[name.lower()] = data
    for key in sorted(written):
        name = next(name for name in output_name.values() if name.lower() == key)
        (files_dir / name).write_bytes(written[key])
    names = {name for name in output_name.values()}
    if corporations:
        compat_name = "Corp_DLCCompat.lua"
        (files_dir / compat_name).write_text(corporations_compat_lua(), encoding="utf-8")
        names.add(compat_name)
    return sorted(names, key=str.lower)


def install_entrypoint_hook(
    manifest: Manifest,
    game_assets: Path | None,
    ui_set: str,
    output: Path,
) -> list[str]:
    entrypoints = [entry for entry in manifest.entry_points if entry.kind == "InGameUIAddin"]
    unsupported = [entry for entry in manifest.entry_points if entry.kind not in ("InGameUIAddin", "MapScript")]
    if unsupported:
        kinds = ", ".join(sorted({entry.kind for entry in unsupported}))
        raise PackError(f"unsupported mod entry point types: {kinds}")
    if not entrypoints:
        return []
    if game_assets is None:
        raise PackError("--game-assets is required to translate InGameUIAddin entry points")
    source = effective_ui_source(game_assets, ui_set, "InGame.lua")
    text = source.read_text(encoding="utf-8-sig")
    injected = ["", "-- BEGIN DLC PACKER MOD ENTRY POINTS"]
    resolved: list[tuple[EntryPoint, PurePosixPath]] = []
    for entry in entrypoints:
        script = resolve_ingame_script(manifest.path.parent, manifest, entry)
        resolved.append((entry, script))
        injected.append(f'include("{script.name.removesuffix(".lua")}");')
    injected.append("-- END DLC PACKER MOD ENTRY POINTS")
    ui_dir = output / "UI"
    ui_dir.mkdir()
    (ui_dir / "InGame.lua").write_text(text.rstrip() + "\n" + "\n".join(injected) + "\n", encoding="utf-8")
    return [
        f"{entry.kind}:{entry.path.as_posix()}"
        + (f"->{script.as_posix()}" if script != entry.path else "")
        for entry, script in resolved
    ]


def copy_maps(mod_dir: Path, manifest: Manifest, output: Path) -> list[str]:
    paths = [item.path for item in manifest.files if item.path.suffix.lower() == ".civ5map"]
    paths.extend(entry.path for entry in manifest.entry_points if entry.kind == "MapScript")
    unique_paths = list(dict.fromkeys(paths))
    if not unique_paths:
        return []
    maps_dir = output / "Maps"
    maps_dir.mkdir()
    written: dict[str, bytes] = {}
    names: list[str] = []
    for rel_path in unique_paths:
        data = resolve_mod_file(mod_dir, rel_path).read_bytes()
        key = rel_path.name.lower()
        if key in written and written[key] != data:
            raise PackError(f"map namespace collision for {rel_path.name}")
        if key not in written:
            (maps_dir / rel_path.name).write_bytes(data)
            written[key] = data
            names.append(rel_path.name)
    return sorted(names, key=str.lower)


def language_values(parent: ET.Element, tag: str, value: str) -> None:
    node = ET.SubElement(parent, tag)
    for lang in ("en_US", "fr_FR", "de_DE", "es_ES", "it_IT", "ru_RU", "ja_JP", "pl_PL", "ko_KR", "zh_Hant_HK"):
        item = ET.SubElement(node, "Value", {"language": lang})
        item.text = value


def write_package(
    output: Path,
    manifest: Manifest,
    dlc_id: uuid.UUID,
    priority: int,
    ui_set: str,
    game_data: Sequence[str],
    text_data: Sequence[str],
    has_files: bool,
    has_ui: bool,
    has_maps: bool,
) -> str:
    root = ET.Element("Civ5Package")
    add_text(root, "GUID", "{" + str(dlc_id) + "}")
    add_text(root, "Version", manifest.version)
    language_values(root, "Name", f"{slug(manifest.name)}_v{manifest.version}")
    language_values(root, "Description", f"{manifest.name} (packed multiplayer DLC)")
    add_text(root, "Priority", priority)
    add_text(root, "SteamApp", 99999)
    add_text(root, "Ownership", "FREE")
    ptags = ET.SubElement(root, "PTags")
    add_text(ptags, "Tag", "Version")
    add_text(ptags, "Tag", "Ownership")
    gameplay = ET.SubElement(root, "Gameplay")
    for name in text_data:
        add_text(gameplay, "TextData", name)
    for name in game_data:
        add_text(gameplay, "GameData", name)
    if has_files:
        add_text(gameplay, "Directory", "Files")
    if has_ui:
        add_text(gameplay, "Directory", "UI")
    if has_maps:
        add_text(gameplay, "MapDirectory", "Maps")
    if has_files or has_ui:
        skin = ET.SubElement(root, "UISkin", {"name": f"Packed{slug(manifest.name)}", "set": ui_set, "platform": "Common"})
        regular = ET.SubElement(skin, "Skin")
        gameplay_skin = ET.SubElement(skin, "GameplaySkin")
        for directory in (["Files"] if has_files else []) + (["UI"] if has_ui else []):
            add_text(regular, "Directory", directory)
            add_text(gameplay_skin, "Directory", directory)
    filename = f"{str(dlc_id).replace('-', '')}_v{manifest.version}.Civ5Pkg"
    write_xml(output / filename, root)
    return filename


def xml_values(node: ET.Element) -> dict[str, str]:
    values = dict(node.attrib)
    for child in node:
        if isinstance(child.tag, str):
            values[local_name(child.tag)] = child.text or ""
    return values


def quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sql_default(value: str) -> str:
    stripped = value.strip()
    if stripped.upper() == "NULL":
        return "NULL"
    if stripped.lower() == "true":
        return "1"
    if stripped.lower() == "false":
        return "0"
    try:
        float(stripped)
        return stripped
    except ValueError:
        return "'" + stripped.replace("'", "''") + "'"


def apply_table_definition(target: sqlite3.Connection, node: ET.Element) -> str:
    table_name = node.get("name")
    if not table_name:
        raise PackError("database Table definition has no name")
    columns = []
    for column in node.findall("Column"):
        name = column.get("name")
        column_type = column.get("type", "text")
        if not name:
            raise PackError(f"database Table {table_name} has a Column without a name")
        parts = [quoted_identifier(name), column_type]
        if column.get("primarykey", "false").lower() == "true":
            parts.append("PRIMARY KEY")
        if column.get("autoincrement", "false").lower() == "true":
            parts.append("AUTOINCREMENT")
        if column.get("notnull", "false").lower() == "true":
            parts.append("NOT NULL")
        if column.get("unique", "false").lower() == "true":
            parts.append("UNIQUE")
        if "default" in column.attrib:
            parts.extend(("DEFAULT", sql_default(column.attrib["default"])))
        if column.get("reference"):
            parts.extend(("REFERENCES", column.get("reference")))
        columns.append(" ".join(parts))
    if not columns:
        raise PackError(f"database Table {table_name} has no columns")
    target.execute(f"CREATE TABLE {quoted_identifier(table_name)} ({','.join(columns)})")
    return table_name


def validate_database_actions(base_db: Path, files: Sequence[Path]) -> None:
    source = sqlite3.connect(f"file:{base_db}?mode=ro", uri=True)
    target = sqlite3.connect(":memory:")
    source.backup(target)
    source.close()
    target.execute("PRAGMA foreign_keys=OFF")
    current_file = Path("<none>")
    current_table = "<none>"
    try:
        for current_file in files:
            root = ET.parse(current_file).getroot()
            for table_node in root:
                if not isinstance(table_node.tag, str):
                    continue
                current_table = local_name(table_node.tag)
                if current_table == "Table":
                    current_table = apply_table_definition(target, table_node)
                    continue
                table_sql = quoted_identifier(current_table)
                for action in table_node:
                    if not isinstance(action.tag, str):
                        continue
                    kind = local_name(action.tag)
                    if kind in ("Row", "Replace"):
                        values = xml_values(action)
                        columns = ",".join(quoted_identifier(key) for key in values)
                        placeholders = ",".join("?" for _ in values)
                        verb = "INSERT OR REPLACE" if kind == "Replace" else "INSERT"
                        target.execute(
                            f"{verb} INTO {table_sql} ({columns}) VALUES ({placeholders})",
                            tuple(values.values()),
                        )
                    elif kind == "Update":
                        where = xml_values(action.find("Where"))
                        values = xml_values(action.find("Set"))
                        if not where or not values:
                            raise PackError(f"invalid empty Update in {current_file.name}:{current_table}")
                        set_sql = ",".join(f"{quoted_identifier(key)}=?" for key in values)
                        where_sql = " AND ".join(f"{quoted_identifier(key)}=?" for key in where)
                        target.execute(
                            f"UPDATE {table_sql} SET {set_sql} WHERE {where_sql}",
                            (*values.values(), *where.values()),
                        )
                    elif kind == "Delete":
                        where = xml_values(action)
                        if where:
                            where_sql = " AND ".join(f"{quoted_identifier(key)}=?" for key in where)
                            target.execute(f"DELETE FROM {table_sql} WHERE {where_sql}", tuple(where.values()))
                        else:
                            target.execute(f"DELETE FROM {table_sql}")
                    else:
                        raise PackError(f"unsupported database XML operation {kind} in {current_file.name}:{current_table}")
        target.rollback()
    except sqlite3.Error as exc:
        raise PackError(f"database validation failed in {current_file.name}:{current_table}: {exc}") from exc
    finally:
        target.close()


def validate_output(
    output: Path,
    package_name: str,
    future_worlds: bool,
    corporations: bool,
    base_db: Path | None = None,
) -> list[str]:
    package = ET.parse(output / package_name).getroot()
    if local_name(package.tag) != "Civ5Package":
        raise PackError("generated package does not have a Civ5Package root")
    checked_xml = 0
    for tag in ("GameData", "TextData"):
        for node in package.findall(f"./Gameplay/{tag}"):
            if not node.text:
                raise PackError(f"generated package contains an empty {tag} entry")
            target = output / node.text
            if not target.is_file():
                raise PackError(f"generated package refers to missing {tag} file: {node.text}")
            if local_name(ET.parse(target).getroot().tag) != "GameData":
                raise PackError(f"generated database file has an invalid root: {node.text}")
            checked_xml += 1
    for node in package.findall("./Gameplay/Directory"):
        if not node.text or not (output / node.text).is_dir():
            raise PackError(f"generated package refers to missing directory: {node.text}")
    for node in package.findall("./Gameplay/MapDirectory"):
        if not node.text or not (output / node.text).is_dir():
            raise PackError(f"generated package refers to missing map directory: {node.text}")
    checks = [
        f"package references resolve ({checked_xml} database XML files)",
        "all generated XML is well-formed",
    ]
    if future_worlds:
        database_files = [output / node.text for tag in ("GameData", "TextData") for node in package.findall(f"./Gameplay/{tag}")]
        if any(root.find(".//SplashScreen") is not None for root in (ET.parse(path).getroot() for path in database_files)):
            raise PackError("Future Worlds compatibility output still contains the unsupported SplashScreen column")
        popup = (output / "Files/NewEraPopup.lua").read_text(encoding="utf-8")
        hook = (output / "UI/InGame.lua").read_text(encoding="utf-8")
        if "GameInfo.Eras[iEra].SplashScreen" in popup or "ERA_FW_FUTURE" not in popup:
            raise PackError("Future Worlds era popup compatibility rewrite is missing")
        if 'include("FutureLua");' not in hook:
            raise PackError("Future Worlds InGameUIAddin hook is missing")
        compat = ET.parse(output / "028_FutureWorlds_SQL_Compatibility.xml").getroot()
        if compat.find("./ArtDefine_UnitInfos/Row") is None or compat.find("./Eras/Update/Where") is None:
            raise PackError("Future Worlds SQL compatibility XML is incomplete")
        if base_db is None:
            raise PackError("internal error: Future Worlds validation requires a base database")
        game_files = [output / node.text for node in package.findall("./Gameplay/GameData")]
        checks.extend(
            [
                "Future Worlds SQL compatibility rows are present",
                "Future Worlds Lua entry point and era-popup rewrite are present",
                "unsupported Eras.SplashScreen column is absent",
            ]
        )
    if corporations:
        game_files = [output / node.text for node in package.findall("./Gameplay/GameData")]
        for path in game_files:
            root = ET.parse(path).getroot()
            for table in root:
                table_name = local_name(table.tag) if isinstance(table.tag, str) else ""
                if table_name not in CORPORATION_SIDECARS:
                    continue
                forbidden = set(CORPORATION_SIDECARS[table_name][2])
                for node in table.iter():
                    if local_name(node.tag) in forbidden or forbidden.intersection(node.attrib):
                        raise PackError(f"Corporations core-table custom field remained in {path.name}:{table_name}")
        compat_path = output / "032_Corporations_SQL_Compatibility.xml"
        compat = ET.parse(compat_path).getroot()
        defined_tables = {node.get("name") for node in compat.findall("Table")}
        expected_tables = {spec[0] for spec in CORPORATION_SIDECARS.values()}
        if not expected_tables.issubset(defined_tables):
            raise PackError("Corporations sidecar table definitions are incomplete")
        helper = (output / "Files/Corp_DLCCompat.lua").read_text(encoding="utf-8")
        entry = (output / "Files/Corporations.lua").read_text(encoding="utf-8")
        if 'include("Corp_DLCCompat.lua");' not in entry or "CorpGetBuildingSetting" not in helper:
            raise PackError("Corporations Lua compatibility layer is not loaded")
        for path in (output / "Files").glob("*.lua"):
            if path.name == "Corp_DLCCompat.lua":
                continue
            text = path.read_text(encoding="utf-8-sig")
            remaining = [field for field in CORPORATION_FIELDS if re.search(rf"\.{re.escape(field)}\b", text)]
            if remaining:
                raise PackError(f"untranslated Corporations Lua fields in {path.name}: {', '.join(remaining)}")
        checks.extend(
            [
                "Corporations core-table custom fields were moved to eight sidecar tables",
                "Corporations Lua configuration lookups use the generated compatibility layer",
            ]
        )
    if base_db is not None:
        game_files = [output / node.text for node in package.findall("./Gameplay/GameData")]
        validate_database_actions(base_db, game_files)
        checks.append("all gameplay database actions execute against the supplied core database")
    return checks


def pack(args: argparse.Namespace) -> BuildReport:
    mod_dir = args.mod.resolve()
    output = args.output.resolve()
    manifest = parse_manifest(mod_dir)
    validate_files(mod_dir, manifest)
    if manifest.dependencies:
        raise PackError(
            "required mod dependencies must be packed together first: " + ", ".join(manifest.dependencies)
        )
    if output == mod_dir or mod_dir in output.parents:
        raise PackError("output must not be the mod directory or a directory inside it")
    if output.exists():
        if not args.force:
            raise PackError(f"output already exists: {output} (pass --force to replace it)")
        if not output.is_dir() or output == Path(output.anchor):
            raise PackError(f"refusing to replace unsafe output path: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    future_worlds = manifest.mod_id == FUTURE_WORLDS_ID and manifest.version == 6
    corporations = manifest.mod_id == CORPORATIONS_ID and manifest.version == 1
    sql_actions = [path for kind, path in manifest.actions if path.suffix.lower() == ".sql"]
    unsupported_actions = [kind for kind, _ in manifest.actions if kind != SUPPORTED_ACTION]
    if unsupported_actions:
        raise PackError(f"unsupported OnModActivated actions: {', '.join(sorted(set(unsupported_actions)))}")
    if sql_actions and not (future_worlds or corporations):
        names = ", ".join(path.as_posix() for path in sql_actions)
        raise PackError(f"SQL cannot be loaded directly by a DLC package; no compatibility compiler for: {names}")
    if future_worlds and args.base_db is None:
        raise PackError("Future Worlds needs --base-db to compile its SQL into DLC XML")
    if corporations and args.base_db is None:
        raise PackError("Corporations needs --base-db to validate its sidecar-table compatibility build")

    game_data: list[str] = []
    text_data: list[str] = []
    omitted: list[str] = []
    warnings: list[str] = []
    corporation_settings: dict[str, dict[str, dict[str, str]]] | None = {} if corporations else None
    if manifest.supports_multiplayer is False:
        warnings.append("the source manifest declares SupportsMultiplayer=0")
    if manifest.supports_mac is False:
        warnings.append("the source manifest declares SupportsMac=0")
    if manifest.references:
        warnings.append("optional referenced mods were not packed: " + ", ".join(manifest.references))
    action_index = 0
    for kind, rel_path in manifest.actions:
        action_index += 1
        source = resolve_mod_file(mod_dir, rel_path)
        if rel_path.suffix.lower() == ".sql":
            continue
        if rel_path.suffix.lower() != ".xml":
            raise PackError(f"unsupported database action file type: {rel_path}")
        try:
            action_root = ET.parse(source).getroot()
        except ET.ParseError as exc:
            raise PackError(f"invalid database XML {rel_path}: {exc}") from exc
        if local_name(action_root.tag) == "LeaderScene":
            imported_paths = {item.path for item in manifest.files if item.imported}
            if rel_path not in imported_paths:
                raise PackError(f"leader scene action is not imported into the virtual filesystem: {rel_path}")
            warnings.append(f"treated {rel_path} as an imported LeaderScene asset")
            continue
        game_files, text_files = copy_database_xml(
            source,
            output,
            action_index,
            rel_path.as_posix(),
            future_worlds,
            corporation_settings,
        )
        game_data.extend(game_files)
        text_data.extend(text_files)

    translated_sql: list[str] = []
    if future_worlds:
        compat_name = f"{action_index + 1:03d}_FutureWorlds_SQL_Compatibility.xml"
        write_xml(output / compat_name, future_worlds_compat_xml(args.base_db.resolve(), mod_dir))
        game_data.append(compat_name)
        translated_sql = [path.as_posix() for path in sql_actions]
        omitted.extend(
            [
                "FutureImprovements.sql dynamic triggers (current farm/plantation resource rows were materialized)",
                "EraSplashScreen.sql custom database column (NewEraPopup.lua uses a static era mapping instead)",
                "EraUpdating.sql localization-key renaming (existing localized era keys are retained)",
            ]
        )
        warnings.append(
            "The package is compiled for the database supplied with --base-db; every multiplayer participant "
            "should use the same Civ V expansion/DLC set."
        )

    if corporations:
        compat_name = f"{action_index + 1:03d}_Corporations_SQL_Compatibility.xml"
        assert corporation_settings is not None
        write_xml(output / compat_name, corporations_compat_xml(corporation_settings))
        game_data.append(compat_name)
        translated_sql = [path.as_posix() for path in sql_actions]
        omitted.append(
            "late ALTER TABLE columns on eight core tables (replaced with DLC-safe Corporation*Settings sidecar tables)"
        )
        warnings.append(
            "Corporations save data uses Modding.OpenSaveData(); host/client save and reload behavior requires runtime testing."
        )

    imported = copy_imports(mod_dir, manifest, output, future_worlds, corporations)
    maps = copy_maps(mod_dir, manifest, output)
    entrypoints = install_entrypoint_hook(manifest, args.game_assets, args.ui_set, output)
    if any("->" in entry for entry in entrypoints):
        warnings.append("translated a GameData XML InGameUIAddin entry to its same-name Lua companion")
    if not (game_data or text_data or imported or maps or entrypoints):
        raise PackError("mod produced no DLC payload")
    dlc_id = uuid.uuid5(PACKER_NAMESPACE, f"{manifest.mod_id}:{manifest.version}:dlc")
    package_name = write_package(
        output,
        manifest,
        dlc_id,
        args.priority,
        args.ui_set,
        game_data,
        text_data,
        bool(imported),
        bool(entrypoints),
        bool(maps),
    )
    validation = validate_output(
        output,
        package_name,
        future_worlds,
        corporations,
        args.base_db.resolve() if args.base_db else None,
    )
    report = BuildReport(
        source_mod=manifest.name,
        source_id=manifest.mod_id,
        source_version=manifest.version,
        dlc_id=str(dlc_id),
        ui_set=args.ui_set,
        game_data=game_data,
        text_data=text_data,
        imported_files=imported,
        maps=maps,
        entry_points=entrypoints,
        translated_sql=translated_sql,
        omitted_features=omitted,
        warnings=warnings,
        validation=validation,
    )
    report_data = dict(report.__dict__)
    report_data["package"] = package_name
    report_data["output_bytes"] = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
    (output / "pack-report.json").write_text(json.dumps(report_data, indent=2) + "\n", encoding="utf-8")
    return report


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mod", type=Path, help="directory containing one ModBuddy .modinfo file")
    parser.add_argument("output", type=Path, help="new DLC directory to create")
    parser.add_argument("--base-db", type=Path, help="Civ5CoreDatabase.db used to compile supported SQL")
    parser.add_argument("--game-assets", type=Path, help="Civ V Assets directory, needed for UI add-ins")
    parser.add_argument("--ui-set", choices=("BaseGame", "Expansion", "Expansion2"), default="Expansion2")
    parser.add_argument("--priority", type=int, default=10)
    parser.add_argument("--force", action="store_true", help="replace the exact output directory if it exists")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        report = pack(args)
    except (PackError, ET.ParseError, sqlite3.Error, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"Packed {report.source_mod} v{report.source_version} as DLC {report.dlc_id}")
    print(f"Output: {args.output.resolve()}")
    print(f"Database files: {len(report.game_data)} gameplay, {len(report.text_data)} text")
    print(f"Imported virtual files: {len(report.imported_files)}")
    print(f"Maps/map scripts: {len(report.maps)}")
    for warning in report.warnings:
        print(f"Warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
