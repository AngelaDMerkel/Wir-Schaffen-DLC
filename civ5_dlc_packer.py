#!/usr/bin/env python3
"""Pack a Civilization V ModBuddy mod as a DLC directory.

The normal Civ V mod loader accepts SQL and InGameUIAddin entry points, while
the DLC package loader does not.  This tool handles the common XML/import case
and contains explicit compatibility compilers for curated complex mods.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


FUTURE_WORLDS_ID = "d9ece224-6cd8-4519-a27a-c417b59cdf35"
CORPORATIONS_ID = "199bbbf8-f80e-449e-a67e-036a7248fb13"
REALLY_ADVANCED_SETUP_ID = "34feb829-33fb-4241-956f-462e6877e070"
MASS_EFFECT_ID = "1a7a5b30-1e1a-4c3d-aff2-7588494f514d"
PACKER_NAMESPACE = uuid.UUID("07186f1e-bfdd-45db-8c1d-f9679e727249")
SUPPORTED_ACTION = "UpdateDatabase"
VERSION = "1.0.10"
CIV5_MAP_MAX_PLOTS = 32768
CIV5_MAP_HEADER_BYTES = 42
# Civ V mixes this fixed 16-byte seed with the protected package fields and
# compares the MD5 digest with <Key>. Without a matching key, the native macOS
# build parses a .Civ5Pkg but excludes it from DownloadableContent entirely.
CIV5_PACKAGE_KEY_SEED = bytes.fromhex("1f3393fb350f42c7bd50be7aa5c26181")
STOCK_WORLD_SIZES = {
    "WORLDSIZE_DUEL",
    "WORLDSIZE_TINY",
    "WORLDSIZE_SMALL",
    "WORLDSIZE_STANDARD",
    "WORLDSIZE_LARGE",
    "WORLDSIZE_HUGE",
}

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


@dataclass(frozen=True)
class Civ5MapInfo:
    path: Path
    format_version: int
    scenario_version: int
    width: int
    height: int
    player_count: int
    flags: int
    map_name: str
    description: str
    world_size: str
    geography_offset: int
    file_bytes: int
    scenario_bytes: int
    sha256: str
    terrain_types: tuple[str, ...]
    feature_types: tuple[str, ...]
    resource_types: tuple[str, ...]

    @property
    def plot_count(self) -> int:
        return self.width * self.height

    @property
    def aspect_ratio(self) -> float:
        return max(self.width, self.height) / min(self.width, self.height)


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


def _decode_map_text(data: bytes) -> str:
    return data.rstrip(b"\0").decode("utf-8", errors="replace")


def _map_type_list(data: bytes) -> tuple[str, ...]:
    return tuple(
        item.decode("ascii", errors="replace")
        for item in data.split(b"\0")
        if item
    )


def inspect_civ5_map(path: Path) -> Civ5MapInfo:
    """Parse and validate the fixed header and geography of a Civ V v11/v12 map."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise PackError(f"map file not found: {path}")
    data = path.read_bytes()
    if len(data) < CIV5_MAP_HEADER_BYTES + 4:
        raise PackError(f"Civ5Map header is truncated: {path}")

    version_byte = data[0]
    format_version = version_byte & 0x0F
    scenario_version = version_byte >> 4
    if format_version not in (11, 12):
        raise PackError(
            f"unsupported Civ5Map format v{format_version}; standalone packaging supports v11-v12"
        )
    width, height = struct.unpack_from("<II", data, 1)
    player_count = data[9]
    flags = struct.unpack_from("<I", data, 10)[0]
    block_lengths = struct.unpack_from("<7I", data, 14)
    if width == 0 or height == 0:
        raise PackError(f"Civ5Map has invalid dimensions {width}x{height}")
    if width * height > CIV5_MAP_MAX_PLOTS:
        raise PackError(
            f"Civ5Map has {width * height:,} plots; the file format limit is {CIV5_MAP_MAX_PLOTS:,}"
        )

    offset = CIV5_MAP_HEADER_BYTES
    blocks: list[bytes] = []
    for length in block_lengths:
        end = offset + length
        if end > len(data):
            raise PackError(f"Civ5Map metadata block is truncated at byte {offset}")
        blocks.append(data[offset:end])
        offset = end
    if offset + 4 > len(data):
        raise PackError("Civ5Map is missing its world-size identifier")
    world_size_length = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    world_size_end = offset + world_size_length
    if world_size_length == 0 or world_size_end > len(data):
        raise PackError("Civ5Map has an invalid world-size identifier")
    world_size = _decode_map_text(data[offset:world_size_end])
    if not re.fullmatch(r"WORLDSIZE_[A-Z0-9_]+", world_size):
        raise PackError(f"Civ5Map has an invalid world-size type: {world_size!r}")
    geography_offset = world_size_end
    geography_bytes = width * height * 8
    geography_end = geography_offset + geography_bytes
    if geography_end > len(data):
        raise PackError(
            f"Civ5Map geography is truncated: expected {geography_bytes:,} bytes at byte {geography_offset}"
        )
    scenario_bytes = len(data) - geography_end
    if scenario_version == 0 and scenario_bytes:
        raise PackError(
            f"non-scenario Civ5Map contains {scenario_bytes:,} unexpected bytes after geography"
        )

    terrain_types = _map_type_list(blocks[0])
    feature_types = _map_type_list(blocks[1])
    resource_types = _map_type_list(blocks[3])
    invalid: list[str] = []
    geography = memoryview(data)[geography_offset:geography_end]
    for plot in range(width * height):
        record = geography[plot * 8:(plot + 1) * 8]
        for label, value, count, allow_none in (
            ("terrain", record[0], len(terrain_types), True),
            ("resource", record[1], len(resource_types), True),
            ("feature", record[2], len(feature_types), True),
        ):
            if value >= count and not (allow_none and value == 0xFF):
                invalid.append(f"plot {plot} has {label} index {value} (list contains {count})")
                break
        if invalid:
            break
    if invalid:
        raise PackError("invalid Civ5Map geography: " + invalid[0])

    return Civ5MapInfo(
        path=path,
        format_version=format_version,
        scenario_version=scenario_version,
        width=width,
        height=height,
        player_count=player_count,
        flags=flags,
        map_name=_decode_map_text(blocks[5]) or path.stem,
        description=_decode_map_text(blocks[6]),
        world_size=world_size,
        geography_offset=geography_offset,
        file_bytes=len(data),
        scenario_bytes=scenario_bytes,
        sha256=hashlib.sha256(data).hexdigest(),
        terrain_types=terrain_types,
        feature_types=feature_types,
        resource_types=resource_types,
    )


def standalone_map_manifest(info: Civ5MapInfo) -> Manifest:
    identity = uuid.uuid5(PACKER_NAMESPACE, f"civ5map:{info.sha256}")
    description = info.description or f"{info.width}x{info.height} Civilization V map"
    return Manifest(
        path=info.path,
        mod_id=str(identity),
        version=1,
        name=info.map_name,
        description=description,
        supports_multiplayer=True,
        supports_mac=True,
        dependencies=(),
        references=(),
        files=(ModFile(PurePosixPath(info.path.name), "", False),),
        actions=(),
        entry_points=(),
    )


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


def add_missing_unit_help(table: ET.Element) -> int:
    """Give newly inserted units a non-null Help key for the BNW production UI."""
    changed = 0
    for row in table:
        if not isinstance(row.tag, str) or local_name(row.tag) != "Row":
            continue
        if child_value(row, "Help"):
            continue
        fallback = child_value(row, "Strategy") or child_value(row, "Description")
        if fallback:
            add_text(row, "Help", fallback)
            changed += 1
    return changed


PROMOTION_PREREQ_COLUMN = re.compile(r"PromotionPrereqOr[0-9]+", re.IGNORECASE)


def translate_promotion_prerequisites(table: ET.Element) -> ET.Element | None:
    """Move non-stock promotion prerequisite columns into BNW's relation table."""
    relation = ET.Element("UnitPromotions_PromotionPrereqOrs")
    for action in list(table):
        kind = local_name(action.tag) if isinstance(action.tag, str) else ""
        if kind not in {"Row", "Replace"}:
            continue
        promotion_type = child_value(action, "Type")
        prerequisites: list[str] = []
        for key in list(action.attrib):
            if PROMOTION_PREREQ_COLUMN.fullmatch(key):
                value = action.attrib.pop(key).strip()
                if value:
                    prerequisites.append(value)
        for child in list(action):
            if PROMOTION_PREREQ_COLUMN.fullmatch(local_name(child.tag)):
                value = (child.text or "").strip()
                action.remove(child)
                if value:
                    prerequisites.append(value)
        if prerequisites and not promotion_type:
            raise PackError("promotion prerequisite compatibility requires a UnitPromotions Type")
        for prerequisite in prerequisites:
            add_row(
                relation,
                {
                    "PromotionType": promotion_type,
                    "PrerequisitePromotion": prerequisite,
                },
            )
    return relation if len(relation) else None


BARE_XML_AMPERSAND = re.compile(r"&(?!amp;|lt;|gt;|quot;|apos;|#[0-9]+;|#x[0-9A-Fa-f]+;)")
MISSING_XML_CLOSE_BRACKET = re.compile(
    r"</([A-Za-z_][A-Za-z0-9_.:-]*)(\r?\n)(?=[ \t]*<)"
)


def repair_bare_xml_ampersands(text: str) -> tuple[str, int]:
    """Escape source ampersands that are illegal in XML without double-escaping entities."""
    return BARE_XML_AMPERSAND.subn("&amp;", text)


def repair_missing_xml_close_brackets(text: str) -> tuple[str, int]:
    """Repair a closing tag whose final bracket was omitted at end of line."""
    return MISSING_XML_CLOSE_BRACKET.subn(r"</\1>\2", text)


def parse_database_xml(source: Path) -> tuple[ET.Element, int, int]:
    raw = source.read_bytes()
    try:
        return ET.fromstring(raw), 0, 0
    except ET.ParseError as original_error:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise PackError(f"invalid database XML {source}: {original_error}") from original_error
        repaired, ampersand_count = repair_bare_xml_ampersands(text)
        repaired, close_bracket_count = repair_missing_xml_close_brackets(repaired)
        if not (ampersand_count or close_bracket_count):
            raise PackError(f"invalid database XML {source}: {original_error}") from original_error
        try:
            return ET.fromstring(repaired), ampersand_count, close_bracket_count
        except ET.ParseError as repaired_error:
            raise PackError(f"invalid database XML {source}: {repaired_error}") from repaired_error


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
    root, _, _ = parse_database_xml(source)
    if local_name(root.tag) != "GameData":
        raise PackError(f"database XML must have a GameData root: {source}")
    game_root = ET.Element("GameData")
    text_root = ET.Element("GameData")
    for table in list(root):
        if not isinstance(table.tag, str):
            continue
        table_name = local_name(table.tag)
        if table_name == "Trait_UnimprovedFeatureYieldChanges":
            table.tag = "Trait_FeatureYieldChanges"
            table_name = "Trait_FeatureYieldChanges"
            for action in table:
                if local_name(action.tag) in {"Row", "Replace"} and child_value(
                    action, "AllowImprovement"
                ) is None:
                    add_text(action, "AllowImprovement", "false")
        promotion_prerequisites = None
        if table_name == "Units":
            add_missing_unit_help(table)
        if table_name == "UnitPromotions":
            promotion_prerequisites = translate_promotion_prerequisites(table)
        if future_worlds and table_name == "Eras":
            for splash in list(table.iter("SplashScreen")):
                parent = next((p for p in table.iter() if splash in list(p)), None)
                if parent is not None:
                    parent.remove(splash)
        if corporation_settings is not None and table_name in CORPORATION_SIDECARS:
            collect_corporation_settings(table, table_name, corporation_settings)
        target = text_root if table_name.startswith("Language_") else game_root
        target.append(table)
        if promotion_prerequisites is not None:
            game_root.append(promotion_prerequisites)
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


def _huge_world_defaults() -> dict[str, object]:
    return {
        "Type": "WORLDSIZE_HUGE",
        "Description": "TXT_KEY_WORLD_HUGE",
        "Help": "TXT_KEY_WORLD_HUGE_HELP",
        "DefaultPlayers": 12,
        "DefaultMinorCivs": 24,
        "FogTilesPerBarbarianCamp": 35,
        "NumNaturalWonders": 7,
        "UnitNameModifier": 0,
        "TargetNumCities": 6,
        "NumFreeBuildingResources": 7,
        "BuildingClassPrereqModifier": 100,
        "MaxConscriptModifier": 75,
        "GridWidth": 128,
        "GridHeight": 80,
        "MaxActiveReligions": 7,
        "TerrainGrainChange": 1,
        "FeatureGrainChange": 1,
        "ResearchPercent": 130,
        "NumCitiesUnhappinessPercent": 60,
        "NumCitiesPolicyCostMod": 5,
        "NumCitiesTechCostMod": 2.5,
        "AdvancedStartPointsMod": 120,
        "EstimatedNumCities": 132,
        "PortraitIndex": 5,
        "IconAtlas": "WORLDSIZE_ATLAS",
    }


def world_compatibility_xml(
    world_size: str,
    width: int,
    height: int,
    default_players: int,
    base_db: Path | None,
    default_minor_civs: int | None = None,
) -> ET.Element:
    row = _huge_world_defaults()
    if base_db is not None:
        if not base_db.is_file():
            raise PackError(f"base database not found: {base_db}")
        conn = sqlite3.connect(f"file:{base_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            source = conn.execute("SELECT * FROM Worlds WHERE Type='WORLDSIZE_HUGE'").fetchone()
            if source is None:
                raise PackError("base database is missing WORLDSIZE_HUGE")
            row = dict(source)
        finally:
            conn.close()
    row.pop("ID", None)
    label_key = f"TXT_KEY_{world_size}"
    help_key = f"{label_key}_HELP"
    scale = (width * height) / (128 * 80)
    row.update(
        {
            "Type": world_size,
            "Description": label_key,
            "Help": help_key,
            "DefaultPlayers": max(2, min(22, default_players or 12)),
            "DefaultMinorCivs": max(
                0,
                min(
                    41,
                    default_minor_civs
                    if default_minor_civs is not None
                    else round(24 * scale),
                ),
            ),
            "GridWidth": width,
            "GridHeight": height,
            "EstimatedNumCities": max(1, round(132 * scale)),
        }
    )
    root = ET.Element("GameData")
    table = ET.SubElement(root, "Worlds")
    replace = ET.SubElement(table, "Replace")
    for key, value in row.items():
        if value is not None:
            add_text(replace, key, value)
    return root


def custom_world_compat_xml(info: Civ5MapInfo, base_db: Path | None) -> ET.Element:
    return world_compatibility_xml(
        info.world_size,
        info.width,
        info.height,
        info.player_count,
        base_db,
    )


def world_compatibility_text_xml(
    world_size: str,
    width: int,
    height: int,
) -> ET.Element:
    root = ET.Element("GameData")
    table = ET.SubElement(root, "Language_en_US")
    label = world_size.removeprefix("WORLDSIZE_").replace("_", " ").title()
    plot_count = width * height
    aspect_ratio = max(width, height) / min(width, height)
    values = (
        (f"TXT_KEY_{world_size}", f"{label} ({width}x{height})"),
        (
            f"TXT_KEY_{world_size}_HELP",
            f"Experimental {width}x{height} map ({plot_count:,} plots; "
            f"aspect ratio {aspect_ratio:.2f}:1).",
        ),
    )
    for tag, text in values:
        replace = ET.SubElement(table, "Replace", {"Tag": tag})
        add_text(replace, "Text", text)
    return root


def custom_world_text_xml(info: Civ5MapInfo) -> ET.Element:
    return world_compatibility_text_xml(info.world_size, info.width, info.height)


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
            "WORLDSIZE_COLOSSAL": {"CorporationSpreadDistance": "30"},
            "WORLDSIZE_EXTREME": {"CorporationSpreadDistance": "30"},
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


def lua_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    if text.casefold() in {"true", "false"}:
        return text.casefold()
    if re.fullmatch(r"-?(?:\d+(?:\.\d*)?|\.\d+)", text):
        return text
    return json.dumps(text, ensure_ascii=False)


def corporations_compat_lua(settings: dict[str, dict[str, dict[str, str]]]) -> str:
    seed_corporation_sql_settings(settings)
    blocks = [
        "-- Generated by civ5-dlc-packer.py for Corporations v1",
        "-- Embeds configuration formerly stored in late ALTER TABLE columns.",
        "-- The values are local to each Civ V Lua context and do not rely on GameInfo sidecars.",
        "",
    ]
    for source_table, (sidecar, key_name, fields) in CORPORATION_SIDECARS.items():
        short_name = sidecar.removeprefix("Corporation").removesuffix("Settings")
        variable = f"g_Corp{short_name}Settings"
        blocks.append(f"local {variable} = {{")
        for type_name, values in sorted(settings.get(sidecar, {}).items()):
            blocks.append(f"  [{json.dumps(type_name)}] = {{")
            for field, value in sorted(values.items()):
                blocks.append(f"    {field} = {lua_literal(value)},")
            blocks.append("  },")
        blocks.append("}")
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
    if "CorpGet" in text or path.as_posix() == "Lua/Corporations.lua":
        include = 'include("Corp_DLCCompat.lua");'
        if include not in text:
            text = include + "\n" + text
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


def patch_future_worlds_city_nearby_data(data: bytes) -> bytes:
    text = data.decode("utf-8-sig")
    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]*)bResourceIsCorrectlyImproved = "
        r"\(tResouceValidImprovementsCityNearbyMapDatas\[iResourceID\]"
        r"\[iImprovementID\] ~= nil\);?[ \t]*\r?$"
    )

    def replacement(match: re.Match[str]) -> str:
        indent = match.group("indent")
        return (
            f"{indent}local tValidImprovements = "
            "tResouceValidImprovementsCityNearbyMapDatas[iResourceID] or {}\n"
            f"{indent}bResourceIsCorrectlyImproved = (tValidImprovements[iImprovementID] ~= nil)"
        )

    text, count = pattern.subn(replacement, text)
    if count != 1:
        raise PackError("Future Worlds CityNearbyMapDatasV4.lua is not the expected v6 file")
    unsafe_lookup = "tResouceValidImprovementsCityNearbyMapDatas[iResourceID][iImprovementID]"
    text = text.replace(
        unsafe_lookup,
        "(tResouceValidImprovementsCityNearbyMapDatas[iResourceID] or {})[iImprovementID]",
    )
    return text.encode("utf-8")


def patch_really_advanced_setup_lua(path: PurePosixPath, data: bytes, version: int) -> bytes:
    text = data.decode("utf-8-sig")
    lookup = "Modding.GetActivatedModVersion(MOD_ID)"
    if lookup not in text:
        return data
    return text.replace(lookup, str(version)).encode("utf-8")


MASS_EFFECT_SQL_PATHS = {
    PurePosixPath("Dummy Building Folder/Mass Effect Dummy Building Tables.sql"),
    PurePosixPath("Dummy Building Folder/Mass Effect Dummy Building IsVisible Set.sql"),
}


def mass_effect_hidden_buildings(
    mod_dir: Path, sql_actions: Sequence[PurePosixPath]
) -> tuple[str, ...]:
    if set(sql_actions) != MASS_EFFECT_SQL_PATHS:
        raise PackError("Mass Effect v7 SQL actions no longer match the compatibility compiler")
    added_columns: set[str] = set()
    hidden_fields: dict[str, set[str]] = {}
    hidden_order: list[str] = []
    for path in sql_actions:
        text = resolve_mod_file(mod_dir, path).read_text(encoding="utf-8-sig")
        text = re.sub(r"--[^\n]*", "", text)
        statements = [statement.strip() for statement in text.split(";") if statement.strip()]
        for statement in statements:
            alter = re.fullmatch(
                r"ALTER\s+TABLE\s+Buildings\s+ADD\s+(IsVisible|PediaVisible)"
                r"\s+boolean\s+DEFAULT\s+1",
                statement,
                flags=re.IGNORECASE,
            )
            if alter:
                added_columns.add(alter.group(1).casefold())
                continue
            update = re.fullmatch(
                r"UPDATE\s+Buildings\s+SET\s+(IsVisible|PediaVisible)\s*=\s*0"
                r"\s+WHERE\s+Type\s*=\s*'([^']+)'",
                statement,
                flags=re.IGNORECASE,
            )
            if update:
                field = update.group(1).casefold()
                building_type = update.group(2)
                if building_type not in hidden_fields:
                    hidden_order.append(building_type)
                hidden_fields.setdefault(building_type, set()).add(field)
                continue
            raise PackError(f"unsupported Mass Effect SQL statement in {path}: {statement}")
    expected_fields = {"isvisible", "pediavisible"}
    if added_columns != expected_fields or not hidden_fields:
        raise PackError("Mass Effect visibility-column SQL is incomplete")
    incomplete = [
        building_type
        for building_type, fields in hidden_fields.items()
        if fields != expected_fields
    ]
    if incomplete:
        raise PackError(
            "Mass Effect visibility SQL does not hide both UI representations for: "
            + ", ".join(incomplete)
        )
    return tuple(hidden_order)


def mass_effect_visibility_lua(hidden_buildings: Sequence[str]) -> str:
    rows = "\n".join(f"  [{json.dumps(value)}] = true," for value in hidden_buildings)
    return (
        "-- BEGIN WIR SCHAFFEN DLC MASS EFFECT VISIBILITY\n"
        "local g_WirSchaffenMassEffectHiddenBuildings = {\n"
        f"{rows}\n"
        "};\n"
        "local function WirSchaffenMassEffectBuildingVisible(building)\n"
        "  return building ~= nil and not "
        "g_WirSchaffenMassEffectHiddenBuildings[building.Type];\n"
        "end\n"
        "-- END WIR SCHAFFEN DLC MASS EFFECT VISIBILITY\n\n"
    )


def patch_mass_effect_lua(
    path: PurePosixPath, data: bytes, hidden_buildings: Sequence[str]
) -> bytes:
    text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    helper = mass_effect_visibility_lua(hidden_buildings)
    if path.name == "CityView.lua":
        original = "if (pCity:IsHasBuilding(buildingID)) and building.IsVisible then"
        replacement = (
            "if (pCity:IsHasBuilding(buildingID)) and "
            "WirSchaffenMassEffectBuildingVisible(building) then"
        )
        if text.count(original) != 1:
            raise PackError("Mass Effect CityView visibility rewrite site changed")
        text = text.replace(original, replacement)
        return (helper + text).encode("utf-8")
    if path.name == "CivilopediaScreen.lua":
        text = re.sub(r",\s*Buildings\.PediaVisible\s*--[^\n]*", "", text)
        text = re.sub(
            r"\s+AND\s+Buildings\.PediaVisible\s*=\s*1",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"^.*PediaVisible.*(?:\n|$)", "", text, flags=re.MULTILINE)
        select = "SELECT Buildings.ID, Buildings.Description, Buildings.PortraitIndex, Buildings.IconAtlas"
        if text.count(select) < 3:
            raise PackError("Mass Effect Civilopedia building query rewrite sites changed")
        text = text.replace(select, select + ", Buildings.Type")
        era_loop = (
            "\t\tfor building in BuildingsByEra(era.Type) do\n"
            "\t\t\tAddArticle(eraID, tableid, building);\n"
            "\t\t\ttableid = tableid + 1;\n"
            "\t\tend"
        )
        era_replacement = (
            "\t\tfor building in BuildingsByEra(era.Type) do\n"
            "\t\t\tif WirSchaffenMassEffectBuildingVisible(building) then\n"
            "\t\t\t\tAddArticle(eraID, tableid, building);\n"
            "\t\t\t\ttableid = tableid + 1;\n"
            "\t\t\tend\n"
            "\t\tend"
        )
        no_tech_loop = (
            "\t\t\tfor building in DB.Query(sql) do\n"
            "\t\t\t\tAddArticle(eraID, tableid, building);\n"
            "\t\t\t\ttableid = tableid + 1;\n"
            "\t\t\tend"
        )
        no_tech_replacement = (
            "\t\t\tfor building in DB.Query(sql) do\n"
            "\t\t\t\tif WirSchaffenMassEffectBuildingVisible(building) then\n"
            "\t\t\t\t\tAddArticle(eraID, tableid, building);\n"
            "\t\t\t\t\ttableid = tableid + 1;\n"
            "\t\t\t\tend\n"
            "\t\t\tend"
        )
        if era_loop not in text or no_tech_loop not in text:
            raise PackError("Mass Effect Civilopedia visibility loops changed")
        text = text.replace(era_loop, era_replacement, 1)
        text = text.replace(no_tech_loop, no_tech_replacement, 1)
        return (helper + text).encode("utf-8")
    return data


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
    really_advanced_setup: bool,
    mass_effect_hidden: Sequence[str] = (),
    corporation_settings: dict[str, dict[str, dict[str, str]]] | None = None,
) -> tuple[list[str], list[str]]:
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
    xml_repairs: list[str] = []
    for item in selected:
        source = resolve_mod_file(mod_dir, item.path)
        data = source.read_bytes()
        if future_worlds and item.path.as_posix() == "Lua/NewEraPopup.lua":
            data = patch_future_worlds_popup(data)
        if future_worlds and item.path.as_posix() == "Lua/CityNearbyMapDatasV4.lua":
            data = patch_future_worlds_city_nearby_data(data)
        if corporations and item.path.suffix.lower() == ".lua":
            data = patch_corporations_lua(item.path, data)
        if really_advanced_setup and item.path.suffix.lower() == ".lua":
            data = patch_really_advanced_setup_lua(item.path, data, manifest.version)
        if mass_effect_hidden and item.path.suffix.lower() == ".lua":
            data = patch_mass_effect_lua(item.path, data, mass_effect_hidden)
        if item.path.suffix.lower() == ".xml":
            try:
                xml_text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                xml_text = ""
            if xml_text:
                repaired, repair_count = repair_bare_xml_ampersands(xml_text)
                if repair_count:
                    data = repaired.encode("utf-8")
                    xml_repairs.append(f"{item.path.as_posix()} ({repair_count})")
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
        if corporation_settings is None:
            raise PackError("internal error: Corporations settings were not collected")
        (files_dir / compat_name).write_text(
            corporations_compat_lua(corporation_settings), encoding="utf-8"
        )
        names.add(compat_name)
    return sorted(names, key=str.lower), xml_repairs


def install_entrypoint_hook(
    manifest: Manifest,
    game_assets: Path | None,
    ui_set: str,
    output: Path,
    write_hook: bool = True,
) -> list[str]:
    entrypoints = [entry for entry in manifest.entry_points if entry.kind == "InGameUIAddin"]
    unsupported = [entry for entry in manifest.entry_points if entry.kind not in ("InGameUIAddin", "MapScript")]
    if unsupported:
        kinds = ", ".join(sorted({entry.kind for entry in unsupported}))
        raise PackError(f"unsupported mod entry point types: {kinds}")
    if not entrypoints:
        return []
    resolved: list[tuple[EntryPoint, PurePosixPath]] = []
    for entry in entrypoints:
        script = resolve_ingame_script(manifest.path.parent, manifest, entry)
        resolved.append((entry, script))
    if write_hook:
        if game_assets is None:
            raise PackError("--game-assets is required to translate InGameUIAddin entry points")
        source = effective_ui_source(game_assets, ui_set, "InGame.lua")
        text = source.read_text(encoding="utf-8-sig")
        injected = [
            "",
            "-- BEGIN DLC PACKER MOD ENTRY POINTS",
            "g_DlcPackerAddins = g_DlcPackerAddins or {};",
        ]
        for _, script in resolved:
            name = script.name.removesuffix(".lua")
            injected.append(
                f'table.insert(g_DlcPackerAddins, ContextPtr:LoadNewContext({json.dumps(name)}));'
            )
        injected.append("-- END DLC PACKER MOD ENTRY POINTS")
        ui_dir = output / "UI"
        ui_dir.mkdir()
        (ui_dir / "InGame.lua").write_text(
            text.rstrip() + "\n" + "\n".join(injected) + "\n", encoding="utf-8"
        )
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


def civ5_package_key(
    dlc_id: uuid.UUID,
    steam_apps: Sequence[object],
    protected_values: Sequence[object],
) -> str:
    """Return the package authentication key expected by Civ V's DLC loader."""
    payload = bytearray(dlc_id.bytes_le)
    for value in (*steam_apps, *protected_values):
        payload.extend(str(value).encode("ascii"))

    mixed = bytearray()
    payload_index = 0
    seed_index = 0
    output_size = max(len(payload) * 2, len(payload) + len(CIV5_PACKAGE_KEY_SEED))
    for index in range(output_size):
        if index % 2 == 0 and payload_index < len(payload):
            mixed.append(payload[payload_index])
            payload_index += 1
        else:
            mixed.append(CIV5_PACKAGE_KEY_SEED[seed_index])
            seed_index = (seed_index + 1) % len(CIV5_PACKAGE_KEY_SEED)
    return hashlib.md5(mixed).hexdigest()


def package_key_from_xml(package: ET.Element) -> str:
    try:
        dlc_id = uuid.UUID((package.findtext("GUID") or "").strip().strip("{}"))
    except ValueError as exc:
        raise PackError("generated package has an invalid GUID") from exc
    steam_apps = [
        (node.text or "").strip() for node in package.findall("SteamApp")
    ]
    if not steam_apps:
        raise PackError("generated package has no SteamApp")
    ptags = package.find("PTags")
    protected_values = []
    if ptags is not None:
        for tag in ptags.findall("Tag"):
            field = (tag.text or "").strip()
            if not field:
                raise PackError("generated package has an empty protected tag")
            protected_values.append((package.findtext(field) or "").strip())
    return civ5_package_key(dlc_id, steam_apps, protected_values)


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
    key = ET.Element("Key")
    key.text = package_key_from_xml(root)
    root.insert(list(root).index(ptags), key)
    for name in text_data:
        add_text(root, "TextData", name)
    for name in game_data:
        add_text(root, "GameData", name)
    gameplay = ET.SubElement(root, "Gameplay")
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
    really_advanced_setup: bool = False,
    mass_effect: bool = False,
    deferred_entrypoints: bool = False,
    base_db: Path | None = None,
) -> list[str]:
    package = ET.parse(output / package_name).getroot()
    if local_name(package.tag) != "Civ5Package":
        raise PackError("generated package does not have a Civ5Package root")
    supplied_key = (package.findtext("Key") or "").strip().lower()
    expected_key = package_key_from_xml(package)
    if supplied_key != expected_key:
        raise PackError("generated package authentication key is missing or invalid")
    checked_xml = 0
    for tag in ("GameData", "TextData"):
        for node in package.findall(f"./{tag}"):
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
        "package authentication key matches the protected manifest fields",
        f"package references resolve ({checked_xml} database XML files)",
        "all generated XML is well-formed",
    ]
    if future_worlds:
        database_files = [output / node.text for tag in ("GameData", "TextData") for node in package.findall(f"./{tag}")]
        if any(root.find(".//SplashScreen") is not None for root in (ET.parse(path).getroot() for path in database_files)):
            raise PackError("Future Worlds compatibility output still contains the unsupported SplashScreen column")
        popup = (output / "Files/NewEraPopup.lua").read_text(encoding="utf-8")
        hook = "" if deferred_entrypoints else (output / "UI/InGame.lua").read_text(encoding="utf-8")
        if "GameInfo.Eras[iEra].SplashScreen" in popup or "ERA_FW_FUTURE" not in popup:
            raise PackError("Future Worlds era popup compatibility rewrite is missing")
        if not deferred_entrypoints and 'ContextPtr:LoadNewContext("FutureLua")' not in hook:
            raise PackError("Future Worlds InGameUIAddin hook is missing")
        nearby = (output / "Files/CityNearbyMapDatasV4.lua").read_text(encoding="utf-8")
        if "tValidImprovements" not in nearby or "or {}" not in nearby:
            raise PackError("Future Worlds resource-improvement nil guard is missing")
        compat = ET.parse(output / "028_FutureWorlds_SQL_Compatibility.xml").getroot()
        if compat.find("./ArtDefine_UnitInfos/Row") is None or compat.find("./Eras/Update/Where") is None:
            raise PackError("Future Worlds SQL compatibility XML is incomplete")
        if base_db is None:
            raise PackError("internal error: Future Worlds validation requires a base database")
        game_files = [output / node.text for node in package.findall("./GameData")]
        checks.extend(
            [
                "Future Worlds SQL compatibility rows are present",
                "Future Worlds Lua entry point and era-popup rewrite are present"
                if not deferred_entrypoints
                else "Future Worlds Lua entry point is ready for the shared UI bridge",
                "Future Worlds resource-improvement lookup is nil-safe for combined mods",
                "unsupported Eras.SplashScreen column is absent",
            ]
        )
    if corporations:
        game_files = [output / node.text for node in package.findall("./GameData")]
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
            if "CorpGet" in text and 'include("Corp_DLCCompat.lua");' not in text:
                raise PackError(f"Corporations compatibility helper is missing in {path.name}'s Lua context")
        checks.extend(
            [
                "Corporations core-table custom fields were moved to eight sidecar tables",
                "Corporations Lua configuration lookups use the generated compatibility layer",
                "Corporations settings are embedded in every affected Lua context",
            ]
        )
    if really_advanced_setup:
        files = output / "Files"
        remaining = [
            path.name
            for path in files.glob("*.lua")
            if "Modding.GetActivatedModVersion(MOD_ID)" in path.read_text(
                encoding="utf-8-sig", errors="replace"
            )
        ]
        if remaining:
            raise PackError(
                "Really Advanced Setup still depends on ModBuddy activation state: "
                + ", ".join(remaining)
            )
        data_manager = (files / "GTAS_DataManager.lua").read_text(encoding="utf-8-sig")
        if not re.search(r'Modding\.OpenUserData\("GTAS_AdvancedSetupMod",\s*15\)', data_manager):
            raise PackError("Really Advanced Setup DLC user-data version rewrite is missing")
        checks.append("Really Advanced Setup uses a fixed DLC user-data version instead of ModBuddy activation state")
    if mass_effect:
        files = output / "Files"
        city_view = (files / "CityView.lua").read_text(encoding="utf-8-sig")
        civilopedia = (files / "CivilopediaScreen.lua").read_text(encoding="utf-8-sig")
        if "building.IsVisible" in city_view or "Buildings.PediaVisible" in civilopedia:
            raise PackError("Mass Effect still depends on late Buildings visibility columns")
        for text in (city_view, civilopedia):
            if "WirSchaffenMassEffectBuildingVisible" not in text:
                raise PackError("Mass Effect DLC-safe visibility filter is missing")
        checks.append("Mass Effect dummy-building visibility uses generated DLC-safe Lua filters")
    if base_db is not None:
        game_files = [output / node.text for node in package.findall("./GameData")]
        validate_database_actions(base_db, game_files)
        checks.append("all gameplay database actions execute against the supplied core database")
    return checks


def prepare_output_directory(output: Path, force: bool) -> None:
    if output.exists():
        if not force:
            raise PackError(f"output already exists: {output} (pass --force to replace it)")
        if not output.is_dir() or output == Path(output.anchor):
            raise PackError(f"refusing to replace unsafe output path: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)


def pack_standalone_map(args: argparse.Namespace) -> BuildReport:
    info = inspect_civ5_map(args.mod)
    output = args.output.resolve()
    if output == info.path.parent or output in info.path.parents:
        raise PackError("output must not replace a directory containing the source map")
    prepare_output_directory(output, args.force)
    manifest = standalone_map_manifest(info)
    base_db = args.base_db.resolve() if args.base_db else None

    game_data: list[str] = []
    text_data: list[str] = []
    if info.world_size not in STOCK_WORLD_SIZES:
        namespace = f"{slug(info.world_size)}_{info.sha256[:8]}"
        game_name = f"001_{namespace}_CustomWorld_Compatibility.xml"
        text_name = f"002_{namespace}_CustomWorld_Text.xml"
        write_xml(output / game_name, custom_world_compat_xml(info, base_db))
        write_xml(output / text_name, custom_world_text_xml(info))
        game_data.append(game_name)
        text_data.append(text_name)

    maps_dir = output / "Maps"
    maps_dir.mkdir()
    destination = maps_dir / info.path.name
    shutil.copyfile(info.path, destination)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != info.sha256:
        raise PackError("standalone map copy did not preserve the source bytes")

    dlc_id = uuid.uuid5(PACKER_NAMESPACE, f"{manifest.mod_id}:map-dlc")
    package_name = write_package(
        output,
        manifest,
        dlc_id,
        args.priority,
        args.ui_set,
        game_data,
        text_data,
        False,
        False,
        True,
    )
    validation = validate_output(output, package_name, False, False, False, False, False, base_db)
    validation.extend(
        [
            f"Civ5Map v{info.format_version} geography contains exactly {info.plot_count:,} plot records",
            f"map payload preserved byte-for-byte (SHA-256 {info.sha256})",
            f"map dimensions preserved at {info.width}x{info.height}",
        ]
    )
    if info.world_size not in STOCK_WORLD_SIZES:
        validation.append(f"registered missing {info.world_size} world metadata")

    warnings: list[str] = []
    if max(info.width, info.height) > 255:
        warnings.append(
            f"{info.width}x{info.height} exceeds 255 on one axis. The DLC fixes world-size database/UI "
            "lookup failures without resizing the map; the native engine's large-axis behavior still requires a runtime test."
        )
    if info.aspect_ratio >= 4:
        warnings.append(
            f"the map has an extreme {info.aspect_ratio:.2f}:1 aspect ratio; minimap rendering and late-game pathfinding "
            "should be monitored during testing"
        )
    warnings.append("every multiplayer participant must install this exact generated map DLC")

    report = BuildReport(
        source_mod=manifest.name,
        source_id=manifest.mod_id,
        source_version=manifest.version,
        dlc_id=str(dlc_id),
        ui_set=args.ui_set,
        game_data=game_data,
        text_data=text_data,
        imported_files=[],
        maps=[info.path.name],
        entry_points=[],
        translated_sql=[],
        omitted_features=[],
        warnings=warnings,
        validation=validation,
    )
    report_data = dict(report.__dict__)
    report_data.update(
        {
            "package": package_name,
            "source_kind": "Civ5Map",
            "map": {
                "format_version": info.format_version,
                "scenario_version": info.scenario_version,
                "width": info.width,
                "height": info.height,
                "plot_count": info.plot_count,
                "player_count": info.player_count,
                "flags": info.flags,
                "world_size": info.world_size,
                "aspect_ratio": info.aspect_ratio,
                "geography_offset": info.geography_offset,
                "scenario_bytes": info.scenario_bytes,
                "sha256": info.sha256,
            },
            "output_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file()),
        }
    )
    (output / "pack-report.json").write_text(json.dumps(report_data, indent=2) + "\n", encoding="utf-8")
    return report


def pack(args: argparse.Namespace) -> BuildReport:
    if getattr(args, "map_file", False):
        return pack_standalone_map(args)
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
    prepare_output_directory(output, args.force)

    future_worlds = manifest.mod_id == FUTURE_WORLDS_ID and manifest.version == 6
    corporations = manifest.mod_id == CORPORATIONS_ID and manifest.version == 1
    really_advanced_setup = manifest.mod_id == REALLY_ADVANCED_SETUP_ID and manifest.version == 15
    mass_effect = manifest.mod_id == MASS_EFFECT_ID and manifest.version == 7
    sql_actions = [path for kind, path in manifest.actions if path.suffix.lower() == ".sql"]
    unsupported_actions = [kind for kind, _ in manifest.actions if kind != SUPPORTED_ACTION]
    if unsupported_actions:
        raise PackError(f"unsupported OnModActivated actions: {', '.join(sorted(set(unsupported_actions)))}")
    if sql_actions and not (future_worlds or corporations or mass_effect):
        names = ", ".join(path.as_posix() for path in sql_actions)
        raise PackError(f"SQL cannot be loaded directly by a DLC package; no compatibility compiler for: {names}")
    if future_worlds and args.base_db is None:
        raise PackError("Future Worlds needs --base-db to compile its SQL into DLC XML")
    if corporations and args.base_db is None:
        raise PackError("Corporations needs --base-db to validate its sidecar-table compatibility build")
    mass_effect_hidden = mass_effect_hidden_buildings(mod_dir, sql_actions) if mass_effect else ()

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
        action_root, ampersand_repairs, close_bracket_repairs = parse_database_xml(source)
        if ampersand_repairs:
            warnings.append(
                f"repaired {ampersand_repairs} illegal bare ampersand"
                f"{'s' if ampersand_repairs != 1 else ''} in {rel_path.as_posix()}"
            )
        if close_bracket_repairs:
            warnings.append(
                f"repaired {close_bracket_repairs} missing XML closing-tag bracket"
                f"{'s' if close_bracket_repairs != 1 else ''} in {rel_path.as_posix()}"
            )
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
            "late ALTER TABLE columns on eight core tables (replaced with embedded Lua settings and "
            "DLC-safe Corporation*Settings sidecar tables)"
        )
        warnings.append(
            "Corporations save data uses Modding.OpenSaveData(); host/client save and reload behavior requires runtime testing."
        )

    if mass_effect:
        translated_sql = [path.as_posix() for path in sql_actions]
        omitted.append(
            "late Buildings.IsVisible/PediaVisible schema columns "
            "(replaced with an equivalent generated Lua visibility set)"
        )
        warnings.append(
            "Mass Effect's two late Buildings visibility columns were translated to DLC-safe UI filters."
        )

    imported, imported_xml_repairs = copy_imports(
        mod_dir,
        manifest,
        output,
        future_worlds,
        corporations,
        really_advanced_setup,
        mass_effect_hidden,
        corporation_settings,
    )
    if imported_xml_repairs:
        warnings.append(
            "repaired illegal bare ampersands in imported XML: " + ", ".join(imported_xml_repairs)
        )
    if really_advanced_setup:
        warnings.append(
            "translated Really Advanced Setup's activated-mod version lookup for always-on DLC loading; "
            "its source still declares multiplayer unsupported"
        )
    maps = copy_maps(mod_dir, manifest, output)
    deferred_entrypoints = bool(getattr(args, "defer_entrypoints", False))
    entrypoints = install_entrypoint_hook(
        manifest,
        args.game_assets,
        args.ui_set,
        output,
        write_hook=not deferred_entrypoints,
    )
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
        bool(entrypoints) and not deferred_entrypoints,
        bool(maps),
    )
    validation = validate_output(
        output,
        package_name,
        future_worlds,
        corporations,
        really_advanced_setup,
        mass_effect,
        deferred_entrypoints,
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("mod", type=Path, help="ModBuddy mod directory, or a .Civ5Map with --map-file")
    parser.add_argument("output", type=Path, help="new DLC directory to create")
    parser.add_argument(
        "--map-file",
        action="store_true",
        help="package the first positional argument as a standalone Civ5Map with world-size compatibility",
    )
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
