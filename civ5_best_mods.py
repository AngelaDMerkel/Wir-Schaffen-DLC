#!/usr/bin/env python3
"""Download and verify AngelaDMerkel's curated Civilization V mod preset."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence


PRESET_ID = "angela-d-merkel-very-best-mods-v1"
PRESET_NAME = "AngelaDMerkel's Very Best Mods"
CIV5_WORKSHOP_APP_ID = 8930
WORKSHOP_DETAILS_URL = (
    "https://api.steampowered.com/ISteamRemoteStorage/"
    "GetPublishedFileDetails/v1/"
)
MAX_DETAILS_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class CuratedSource:
    published_file_id: str
    title: str
    workshop_title: str
    manifest_id: str
    manifest_version: int
    manifest_name: str
    source_url: str
    workshop_url: str


@dataclass(frozen=True)
class WorkshopFile:
    source: CuratedSource
    file_url: str
    filename: str
    file_size: int
    content_id: str


@dataclass(frozen=True)
class DownloadedMod:
    source: CuratedSource
    path: Path
    workshop_filename: str
    workshop_content_id: str
    archive_sha256: str
    archive_bytes: int


@dataclass(frozen=True)
class DownloadProgress:
    phase: str
    position: int
    total_items: int
    title: str
    completed_bytes: int = 0
    total_bytes: int = 0
    bytes_per_second: float | None = None
    detail: str = ""


SOURCES = (
    CuratedSource(
        published_file_id="450107332",
        title="Brotherhood of Steel",
        workshop_title="Brotherhood of Steel",
        manifest_id="577db60c-e9e1-4e16-9b48-e8324e551b46",
        manifest_version=2,
        manifest_name="Brotherhood of Steel",
        source_url="https://steamcommunity.com/sharedfiles/filedetails/?id=450107332",
        workshop_url="https://steamcommunity.com/sharedfiles/filedetails/?id=450107332",
    ),
    CuratedSource(
        published_file_id="1263465924",
        title="Really Raging Barbarians",
        workshop_title="Really Raging Barbarians",
        manifest_id="a9de8f53-925d-41af-a3d1-dd2aade3ce68",
        manifest_version=1,
        manifest_name="Really Raging Barbarians",
        source_url="https://steamcommunity.com/sharedfiles/filedetails/?id=1263465924",
        workshop_url="https://steamcommunity.com/sharedfiles/filedetails/?id=1263465924",
    ),
    CuratedSource(
        published_file_id="172933304",
        title="Mass Effect Civilizations",
        workshop_title="[BNW] Mass Effect Civilizations",
        manifest_id="1a7a5b30-1e1a-4c3d-aff2-7588494f514d",
        manifest_version=7,
        manifest_name="BNW Mass Effect",
        source_url="https://steamcommunity.com/sharedfiles/filedetails/?id=172933304",
        workshop_url="https://steamcommunity.com/sharedfiles/filedetails/?id=172933304",
    ),
    CuratedSource(
        published_file_id="233614126",
        title="Workable Mountains",
        workshop_title="Workable Mountains",
        manifest_id="bd94e758-ebf6-4a3d-8aeb-0b7f7f858317",
        manifest_version=2,
        manifest_name="Workable Mountains",
        source_url="https://forums.civfanatics.com/threads/workable-mountains.522546/",
        workshop_url="https://steamcommunity.com/sharedfiles/filedetails/?id=233614126",
    ),
    CuratedSource(
        published_file_id="596919865",
        title="Future Worlds",
        workshop_title="Future Worlds",
        manifest_id="d9ece224-6cd8-4519-a27a-c417b59cdf35",
        manifest_version=6,
        manifest_name="Future Worlds",
        source_url="https://steamcommunity.com/sharedfiles/filedetails/?id=596919865",
        workshop_url="https://steamcommunity.com/sharedfiles/filedetails/?id=596919865",
    ),
    CuratedSource(
        published_file_id="171044751",
        title="Corporations (Brave New World)",
        workshop_title="Corporations (Brave New World)",
        manifest_id="199bbbf8-f80e-449e-a67e-036a7248fb13",
        manifest_version=1,
        manifest_name="Corporations (Brave New World)",
        source_url="https://steamcommunity.com/sharedfiles/filedetails/?id=171044751",
        workshop_url="https://steamcommunity.com/sharedfiles/filedetails/?id=171044751",
    ),
    CuratedSource(
        published_file_id="126959669",
        title="Really Advanced Setup",
        workshop_title="Really Advanced Setup",
        manifest_id="34feb829-33fb-4241-956f-462e6877e070",
        manifest_version=15,
        manifest_name="Really Advanced Setup",
        source_url="https://forums.civfanatics.com/threads/really-advanced-setup.486324/",
        workshop_url="https://steamcommunity.com/sharedfiles/filedetails/?id=126959669",
    ),
)


def _read_response(response: object, limit: int) -> bytes:
    data = response.read(limit + 1)  # type: ignore[attr-defined]
    if len(data) > limit:
        raise DownloadError("the Workshop metadata response is unexpectedly large")
    return data


def fetch_workshop_files(
    sources: Sequence[CuratedSource] = SOURCES,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 30,
) -> list[WorkshopFile]:
    fields: list[tuple[str, str]] = [("itemcount", str(len(sources)))]
    fields.extend(
        (f"publishedfileids[{index}]", source.published_file_id)
        for index, source in enumerate(sources)
    )
    request = urllib.request.Request(
        WORKSHOP_DETAILS_URL,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        headers={"User-Agent": "Wir-Schaffen-DLC"},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(_read_response(response, MAX_DETAILS_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DownloadError(f"could not query Valve's Workshop API: {exc}") from exc

    try:
        details = payload["response"]["publishedfiledetails"]
    except (KeyError, TypeError) as exc:
        raise DownloadError("Valve's Workshop API returned an invalid response") from exc
    if not isinstance(details, list):
        raise DownloadError("Valve's Workshop API returned an invalid item list")
    by_id = {
        str(item.get("publishedfileid")): item
        for item in details
        if isinstance(item, dict)
    }

    result: list[WorkshopFile] = []
    for source in sources:
        item = by_id.get(source.published_file_id)
        if item is None or int(item.get("result", 0)) != 1:
            raise DownloadError(f"Workshop item {source.published_file_id} is unavailable")
        if int(item.get("consumer_app_id", 0)) != CIV5_WORKSHOP_APP_ID:
            raise DownloadError(
                f"Workshop item {source.published_file_id} is not a Civilization V mod"
            )
        if str(item.get("title", "")) != source.workshop_title:
            raise DownloadError(
                f"Workshop item {source.published_file_id} changed identity: "
                f"expected {source.workshop_title!r}"
            )
        filename = str(item.get("filename", ""))
        file_url = str(item.get("file_url", ""))
        content_id = str(item.get("hcontent_file", ""))
        try:
            file_size = int(item.get("file_size", 0))
        except (TypeError, ValueError) as exc:
            raise DownloadError(f"Workshop item {source.published_file_id} has an invalid size") from exc
        parsed = urllib.parse.urlparse(file_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".steamusercontent.com")
        ):
            raise DownloadError(
                f"Workshop item {source.published_file_id} has an unsafe download URL"
            )
        if not filename.lower().endswith(".civ5mod"):
            raise DownloadError(
                f"Workshop item {source.published_file_id} is not a .civ5mod archive"
            )
        if file_size <= 0 or file_size > MAX_ARCHIVE_BYTES:
            raise DownloadError(
                f"Workshop item {source.published_file_id} has an unsupported size: {file_size}"
            )
        if not content_id.isdigit():
            raise DownloadError(f"Workshop item {source.published_file_id} has no content ID")
        result.append(WorkshopFile(source, file_url, filename, file_size, content_id))
    return result


def download_archive(
    item: WorkshopFile,
    destination: Path,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 120,
    progress_fn: Callable[[int, int, float | None], None] | None = None,
) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part")
    digest = hashlib.sha256()
    total = 0
    started_at = time.monotonic()
    last_reported_at = started_at
    request = urllib.request.Request(item.file_url, headers={"User-Agent": "Wir-Schaffen-DLC"})
    try:
        with opener(request, timeout=timeout) as response, temporary.open("wb") as output:
            if progress_fn is not None:
                progress_fn(0, item.file_size, None)
            while True:
                block = response.read(1024 * 1024)  # type: ignore[attr-defined]
                if not block:
                    break
                total += len(block)
                if total > item.file_size or total > MAX_ARCHIVE_BYTES:
                    raise DownloadError(f"{item.source.title} exceeded its advertised size")
                digest.update(block)
                output.write(block)
                now = time.monotonic()
                if progress_fn is not None and (
                    total == item.file_size or now - last_reported_at >= 0.1
                ):
                    elapsed = max(0.001, now - started_at)
                    progress_fn(total, item.file_size, total / elapsed)
                    last_reported_at = now
            output.flush()
            os.fsync(output.fileno())
        if total != item.file_size:
            raise DownloadError(
                f"{item.source.title} download is incomplete: expected {item.file_size}, got {total} bytes"
            )
        os.replace(temporary, destination)
    except OSError as exc:
        raise DownloadError(f"could not download {item.source.title}: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest.hexdigest(), total


def archive_entries(archive: Path, tar_command: str | None = None) -> list[str]:
    command = tar_command or shutil.which("bsdtar") or shutil.which("tar")
    if command is None:
        raise DownloadError("a libarchive-compatible bsdtar command is required to extract .civ5mod files")
    try:
        result = subprocess.run(
            [command, "-tf", str(archive)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise DownloadError(f"could not inspect {archive.name}: {detail.strip()}") from exc
    entries: list[str] = []
    seen: set[str] = set()
    for raw_name in result.stdout.splitlines():
        name = raw_name.rstrip("/")
        path = PurePosixPath(name)
        if (
            not name
            or len(name) > 1024
            or path.is_absolute()
            or "\\" in name
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise DownloadError(f"unsafe path in {archive.name}: {raw_name!r}")
        key = name.casefold()
        if key in seen:
            raise DownloadError(f"duplicate path in {archive.name}: {name}")
        seen.add(key)
        entries.append(name)
    if not entries:
        raise DownloadError(f"{archive.name} is empty")
    return entries


def extract_archive(archive: Path, destination: Path, tar_command: str | None = None) -> None:
    command = tar_command or shutil.which("bsdtar") or shutil.which("tar")
    if command is None:
        raise DownloadError("a libarchive-compatible bsdtar command is required to extract .civ5mod files")
    archive_entries(archive, command)
    destination.mkdir(parents=True)
    try:
        subprocess.run(
            [
                command,
                "-xf",
                str(archive),
                "-C",
                str(destination),
                "--no-same-owner",
                "--no-same-permissions",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise DownloadError(f"could not extract {archive.name}: {detail.strip()}") from exc
    root = destination.resolve()
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise DownloadError(f"archive contains a symbolic link: {path.relative_to(destination)}")
        if not path.resolve().is_relative_to(root):
            raise DownloadError(f"archive escaped its extraction directory: {path}")


def validate_manifest(source: CuratedSource, mod_dir: Path) -> None:
    manifests = sorted(mod_dir.glob("*.modinfo"))
    if len(manifests) != 1:
        raise DownloadError(
            f"{source.title} contains {len(manifests)} root ModBuddy manifests instead of one"
        )
    try:
        root = ET.parse(manifests[0]).getroot()
        version = int(root.get("version", ""))
    except (ET.ParseError, TypeError, ValueError) as exc:
        raise DownloadError(f"{source.title} has an invalid ModBuddy manifest") from exc
    properties = root.find("Properties")
    name = properties.findtext("Name") if properties is not None else None
    if (
        root.tag.rsplit("}", 1)[-1] != "Mod"
        or str(root.get("id", "")).lower() != source.manifest_id
        or version != source.manifest_version
        or name != source.manifest_name
    ):
        raise DownloadError(f"{source.title} source manifest no longer matches the curated preset")


def download_preset(
    destination: Path,
    output_fn: Callable[[str], None] = print,
    opener: Callable[..., object] = urllib.request.urlopen,
    tar_command: str | None = None,
    progress_fn: Callable[[DownloadProgress], None] | None = None,
) -> list[DownloadedMod]:
    output_fn(f"\nDownloading {PRESET_NAME} from {len(SOURCES)} authoritative sources...")
    if progress_fn is not None:
        progress_fn(
            DownloadProgress(
                phase="DOWNLOAD",
                position=0,
                total_items=len(SOURCES),
                title="Valve Workshop catalogue",
                detail="Querying authoritative source metadata",
            )
        )
    items = fetch_workshop_files(opener=opener)
    downloads = destination / "downloads"
    mods = destination / "mods"
    result: list[DownloadedMod] = []
    for position, item in enumerate(items, 1):
        output_fn(f"\n→ [{position}/{len(items)}] Downloading {item.source.title}...")
        archive = downloads / f"{item.source.published_file_id}.civ5mod"
        def report_transfer(
            completed: int,
            total: int,
            speed: float | None,
            *,
            current_position: int = position,
            current_item: WorkshopFile = item,
        ) -> None:
            if progress_fn is not None:
                progress_fn(
                    DownloadProgress(
                        phase="DOWNLOAD",
                        position=current_position,
                        total_items=len(items),
                        title=current_item.source.title,
                        completed_bytes=completed,
                        total_bytes=total,
                        bytes_per_second=speed,
                        detail="Receiving Workshop archive",
                    )
                )

        digest, size = download_archive(
            item,
            archive,
            opener=opener,
            progress_fn=report_transfer,
        )
        if progress_fn is not None:
            progress_fn(
                DownloadProgress(
                    phase="VERIFY",
                    position=position,
                    total_items=len(items),
                    title=item.source.title,
                    completed_bytes=0,
                    total_bytes=size,
                    detail="Inspecting archive and ModBuddy identity",
                )
            )
        mod_dir = mods / item.source.published_file_id
        extract_archive(archive, mod_dir, tar_command=tar_command)
        validate_manifest(item.source, mod_dir)
        if progress_fn is not None:
            progress_fn(
                DownloadProgress(
                    phase="VERIFY",
                    position=position,
                    total_items=len(items),
                    title=item.source.title,
                    completed_bytes=size,
                    total_bytes=size,
                    detail="Archive and manifest verified",
                )
            )
        output_fn(f"  ✓ Verified {item.filename} ({size / (1024 * 1024):.1f} MiB).")
        result.append(
            DownloadedMod(
                source=item.source,
                path=mod_dir,
                workshop_filename=item.filename,
                workshop_content_id=item.content_id,
                archive_sha256=digest,
                archive_bytes=size,
            )
        )
    return result


def collection_metadata(downloaded: Sequence[DownloadedMod]) -> dict[str, object]:
    if [item.source for item in downloaded] != list(SOURCES):
        raise DownloadError("the curated preset is incomplete or out of order")
    return {
        "id": PRESET_ID,
        "name": PRESET_NAME,
        "exclusive": True,
        "members": [
            {
                "title": item.source.title,
                "published_file_id": item.source.published_file_id,
                "manifest_id": item.source.manifest_id,
                "manifest_version": item.source.manifest_version,
                "source_url": item.source.source_url,
                "workshop_url": item.source.workshop_url,
                "workshop_filename": item.workshop_filename,
                "workshop_content_id": item.workshop_content_id,
                "archive_sha256": item.archive_sha256,
                "archive_bytes": item.archive_bytes,
            }
            for item in downloaded
        ],
    }
