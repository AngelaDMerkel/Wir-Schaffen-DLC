#!/usr/bin/env python3

import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import civ5_best_mods as BEST


class FakeResponse:
    def __init__(self, data: bytes):
        self.source = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self.source.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class BestModsTests(unittest.TestCase):
    def workshop_payload(self) -> bytes:
        details = []
        for source in BEST.SOURCES:
            details.append(
                {
                    "publishedfileid": source.published_file_id,
                    "result": 1,
                    "consumer_app_id": BEST.CIV5_WORKSHOP_APP_ID,
                    "title": source.workshop_title,
                    "filename": f"{source.manifest_name} (v {source.manifest_version}).civ5mod",
                    "file_size": 123,
                    "file_url": f"https://cdn.steamusercontent.com/ugc/{source.published_file_id}/hash/",
                    "hcontent_file": source.published_file_id,
                }
            )
        return json.dumps(
            {"response": {"result": 1, "resultcount": len(details), "publishedfiledetails": details}}
        ).encode("utf-8")

    def test_workshop_metadata_is_verified_in_curated_order(self):
        files = BEST.fetch_workshop_files(
            opener=lambda request, timeout: FakeResponse(self.workshop_payload())
        )
        self.assertEqual(
            [item.source.published_file_id for item in files],
            [source.published_file_id for source in BEST.SOURCES],
        )
        self.assertTrue(all(item.file_size == 123 for item in files))

    def test_rejects_a_workshop_item_for_the_wrong_game(self):
        payload = json.loads(self.workshop_payload())
        payload["response"]["publishedfiledetails"][0]["consumer_app_id"] = 289070
        with self.assertRaisesRegex(BEST.DownloadError, "not a Civilization V mod"):
            BEST.fetch_workshop_files(
                opener=lambda request, timeout: FakeResponse(json.dumps(payload).encode("utf-8"))
            )

    def test_download_requires_the_exact_advertised_size_and_hashes_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            source = BEST.SOURCES[0]
            item = BEST.WorkshopFile(
                source,
                "https://cdn.steamusercontent.com/ugc/1/hash/",
                "source.civ5mod",
                7,
                "1",
            )
            destination = Path(temp) / "source.civ5mod"
            progress: list[tuple[int, int, float | None]] = []
            digest, size = BEST.download_archive(
                item,
                destination,
                opener=lambda request, timeout: FakeResponse(b"payload"),
                progress_fn=lambda completed, total, speed: progress.append(
                    (completed, total, speed)
                ),
            )
            self.assertEqual(size, 7)
            self.assertEqual(destination.read_bytes(), b"payload")
            self.assertEqual(
                digest,
                "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5",
            )
            self.assertEqual(progress[0], (0, 7, None))
            self.assertEqual(progress[-1][:2], (7, 7))
            self.assertGreater(progress[-1][2], 0)

    @unittest.skipUnless(shutil.which("bsdtar") or shutil.which("tar"), "tar unavailable")
    def test_archive_listing_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "unsafe.civ5mod"
            with tarfile.open(archive, "w") as output:
                info = tarfile.TarInfo("../escape.modinfo")
                info.size = 3
                output.addfile(info, io.BytesIO(b"bad"))
            with self.assertRaisesRegex(BEST.DownloadError, "unsafe path"):
                BEST.archive_entries(archive)

    def test_manifest_identity_must_match_the_curated_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = BEST.SOURCES[0]
            (root / "source.modinfo").write_text(
                f"<Mod id='{source.manifest_id}' version='{source.manifest_version}'>"
                f"<Properties><Name>{source.manifest_name}</Name></Properties></Mod>",
                encoding="utf-8",
            )
            BEST.validate_manifest(source, root)
            wrong = replace(source, manifest_version=source.manifest_version + 1)
            with self.assertRaisesRegex(BEST.DownloadError, "no longer matches"):
                BEST.validate_manifest(wrong, root)

    def test_collection_metadata_requires_every_member(self):
        downloaded = [
            BEST.DownloadedMod(source, Path(source.published_file_id), "source.civ5mod", "1", "a" * 64, 1)
            for source in BEST.SOURCES
        ]
        metadata = BEST.collection_metadata(downloaded)
        self.assertEqual(metadata["id"], BEST.PRESET_ID)
        self.assertTrue(metadata["exclusive"])
        self.assertEqual(len(metadata["members"]), len(BEST.SOURCES))
        with self.assertRaisesRegex(BEST.DownloadError, "incomplete"):
            BEST.collection_metadata(downloaded[:-1])


if __name__ == "__main__":
    unittest.main()
