import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import document_control as control
from app import ingest_construction as ingest
from app import search_construction as search


class DocumentControlTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.index = self.data / "construction_chunks.jsonl"
        self.register = self.data / "construction_control.sqlite3"
        self.enterContext(patch.multiple(
            control, ROOT=self.root, DATA=self.data, ARCHIVE=self.data/"archive",
            OUTPUT=self.index, REGISTER=self.register, SOURCE_DIR=self.root,
        ))
        self.enterContext(patch.multiple(ingest, ROOT=self.root, SOURCE_DIR=self.root, OUTPUT=self.index))
        self.enterContext(patch.object(search, "CHUNKS_PATH", self.index))
        self.db = sqlite3.connect(self.register)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(control.SCHEMA)

    def deliver(self, project, revision, days, doc="DOC-009"):
        folder = self.data / "inbox" / project
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{doc}_{revision}.md"
        path.write_text(f"# 材料报审流程\n\n## 4.1 归档时限\n\n须在 {days} 个工作日内归档。\n",
                        encoding="utf-8")
        return path

    def test_batch_receipt_idempotent_and_same_revision_edits_rejected(self):
        delivery = self.deliver("QL01", "R01", 2)
        self.deliver("QL01", "R01", 3, "DOC-010")
        self.assertEqual(control.receive(self.db, "QL01", None, "clerk"), (2, 0))
        self.assertEqual(control.receive(self.db, "QL01", None, "clerk"), (0, 2))
        self.assertTrue(all(r[0] == "draft" for r in self.db.execute("SELECT status FROM versions")))
        self.deliver("QL01", "R01", 9, "DOC-001")
        self.deliver("QL01", "R01", 5)
        with self.assertRaisesRegex(ValueError, "已登记不同内容"):
            control.receive(self.db, "QL01", delivery.parent, "clerk")
        self.assertEqual(self.db.execute("SELECT count(*) FROM versions").fetchone()[0], 2)

    def test_approval_switches_current_and_preserves_citation_and_history(self):
        delivery = self.deliver("QL01", "R01", 2)
        self.deliver("QL01", "R02", 3)
        control.receive(self.db, "QL01", None, "clerk")
        control.approve(self.db, ("QL01", "DOC-009", "R01"), "reviewer")
        control.publish(self.db)
        old = search.search_current("QL01", "归档时限")
        self.assertEqual([r["revision"] for r in old], ["R01"])
        delivery.write_text("收件箱中的副本已改变", encoding="utf-8")
        self.assertIn("2 个工作日", (self.root / old[0]["source"]).read_text(encoding="utf-8"))
        self.assertTrue(control.approve(self.db, ("QL01", "DOC-009", "R02"), "reviewer"))
        self.assertFalse(control.approve(self.db, ("QL01", "DOC-009", "R02"), "reviewer"))
        ingest.main()  # Running the old entry point after migration also uses the DB.
        current = search.search_current("QL01", "归档时限")
        self.assertEqual([r["revision"] for r in current], ["R02"])
        self.assertIn("3 个工作日", current[0]["text"])
        self.assertEqual(self.db.execute("SELECT status FROM versions WHERE revision='R01'").fetchone()[0],
                         "superseded")
        self.assertEqual([r[0] for r in self.db.execute("SELECT action FROM events ORDER BY id")],
                         ["received:draft", "received:draft", "approved", "superseded", "approved"])

    def test_migration_error_has_location_and_preserves_previous_index(self):
        bad = self.root / "catalog.json"
        bad.write_text('{\n  project_id TEST,\n}', encoding="utf-8")
        self.index.write_bytes(b"previous index")
        with self.assertRaisesRegex(ValueError, "catalog.json.*line 2 column 3"):
            control.migrate(self.db)
        self.assertEqual(self.index.read_bytes(), b"previous index")
        with self.assertRaisesRegex(ValueError, "catalog.json 第 2 行第 3 列"):
            ingest.main()

    def test_project_filter_separates_same_doc_id_and_title(self):
        for project, days in (("QL01", 2), ("HT02", 5)):
            self.deliver(project, "R01", days)
            control.receive(self.db, project, None, "clerk")
            control.approve(self.db, (project, "DOC-009", "R01"), "reviewer")
        control.publish(self.db)
        for project, days in (("QL01", 2), ("HT02", 5)):
            with self.subTest(project=project):
                hits = search.search_current(project, "材料报审归档时限")
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["project_id"], project)
                self.assertIn(f"{days} 个工作日", hits[0]["text"])

    def test_legacy_migration_retains_status_and_section_metadata(self):
        source = self.root / "OLD.md"
        source.write_text("## 2.1 证据\n旧台账仅在台账中填写文件标题。\n", encoding="utf-8")
        catalog = {"project_id": "QL01", "document_id": "DOC-009", "title": "登记标题",
                   "revisions": [{"revision": "R01", "status": "effective", "source": source.name}]}
        (self.root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        self.assertEqual(control.migrate(self.db), 1)
        self.assertEqual(control.migrate(self.db), 0)
        self.assertEqual(control.publish(self.db), 1)
        row = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual((row["title"], row["status"], row["source_line"]), ("登记标题", "effective", 1))


if __name__ == "__main__":
    unittest.main()
