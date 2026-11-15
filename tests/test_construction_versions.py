import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import ingest_construction as ingest


def write_effective_catalog(path: Path, revision: str) -> None:
    source = path.with_name(f"{revision}.md")
    source.write_text(
        "## 1.1 归档职责\n由资料员归档。\n\n"
        "## 1.2 归档位置\n存入项目目录。\n",
        encoding="utf-8",
    )
    catalog = {
        "project_id": "TEST",
        "document_id": "DOC-01",
        "title": "归档流程",
        "revisions": [
            {"revision": revision, "status": "effective", "source": source.name}
        ],
    }
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")


class ConstructionVersionTests(unittest.TestCase):
    def test_conflicting_revisions_preserve_previous_index(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            index = root / "chunks.jsonl"

            with (
                patch.object(ingest, "ROOT", root),
                patch.object(ingest, "SOURCE_DIR", root),
                patch.object(ingest, "OUTPUT", index),
            ):
                write_effective_catalog(root / "catalog.json", "R01")
                ingest.main()
                previous = index.read_bytes()
                self.assertEqual(len(previous.splitlines()), 2)

                write_effective_catalog(root / "catalog_r02.json", "R02")
                with self.assertRaisesRegex(ValueError, "TEST/DOC-01 存在多个现行版本"):
                    ingest.main()

                self.assertEqual(index.read_bytes(), previous)