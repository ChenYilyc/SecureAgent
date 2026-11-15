import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import demo_projects as library, document_control as control
from app import search_construction as finder, workbench_accounts as accounts
from app.main import app


QUESTION = "接收方签收移交清单后，谁要在几个工作日内归档？"


class ProjectLibraryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.index = self.root / "index.jsonl"
        self.enterContext(patch.multiple(control, ROOT=self.root, REGISTER=self.root / "registry.db",
                                        ARCHIVE=self.root / "archive", OUTPUT=self.index))
        self.enterContext(patch.object(finder, "CHUNKS_PATH", self.index))
        self.enterContext(patch.dict("os.environ", {"SECUREAGENT_ALLOW_REMOTE_QA": "0",
                                                   "SECUREAGENT_RETRIEVAL": "lexical"}))
        self.db = self.enterContext(closing(accounts.connect()))

    def add_existing_document(self, project):
        raw = "# 已登记资料\n\n## 1.1 经办岗位\n\n由资料管理员登记。".encode()
        with control.transaction(self.db):
            control.record_version(self.db, (project, "DOC-01", "R01"), raw, "effective", "fixture")

    def test_snapshot_keeps_existing_projects_and_finds_each_current_rule_and_receipt(self):
        self.add_existing_document("QL01")
        created, names = library.import_library(self.db)
        self.assertEqual(created, 20)
        self.assertEqual(set(names), {"XR03", "BH04", "SG05"})
        control.publish(self.db)
        rows = [json.loads(line) for line in self.index.read_text(encoding="utf-8").splitlines()]
        self.assertIn("QL01/DOC-01/R01/1.1", {row["chunk_id"] for row in rows})
        cases = (
            ("XR03", "XR-DC-101", "R01", "2 个工作日", "XR-JJ-061", "XR-RC-306", "公共区门牌对照表"),
            ("BH04", "BH-DC-101", "R02", "3 个工作日", "BH-CG-042", "BH-RC-307", "西段电子成果目录页"),
            ("SG05", "SG-DC-101", "R01", "1 个工作日", "SG-YW-033", "SG-RC-308", "冷站维护点位索引"),
        )
        for project, doc, rev, days, batch, receipt, item in cases:
            with self.subTest(project=project):
                rule = finder.search_current(project, QUESTION)[0]
                self.assertEqual(rule["chunk_id"], f"{project}/{doc}/{rev}/2.1")
                self.assertIn(days, rule["text"])
                record = finder.search_current(project, f"{batch} 回执登记的待补附件是什么，由谁补交？")[0]
                self.assertEqual(record["chunk_id"], f"{project}/{receipt}/R01/2.1")
                self.assertEqual((record["source_page"], record["source_page_end"]), (2, 2))
                self.assertIn(item, record["text"])
                original = library.LIBRARY / project / (receipt + "_R01.pdf")
                self.assertEqual((self.root / record["source"]).read_bytes(), original.read_bytes())

    def test_reimport_preserves_reviewers_decision_and_does_not_duplicate_history(self):
        library.import_library(self.db)
        control.approve(self.db, ("XR03", "XR-DC-101", "R02"), "fixture-reviewer", expected_current="R01")
        events = self.db.execute("SELECT count(*) FROM events").fetchone()[0]
        created, _ = library.import_library(self.db)
        self.assertEqual(created, 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events").fetchone()[0], events)
        control.publish(self.db)
        current = finder.search_current("XR03", QUESTION)[0]
        self.assertEqual(current["chunk_id"], "XR03/XR-DC-101/R02/2.1")
        self.assertIn("1 个工作日", current["text"])
        self.assertIn("3 个工作日", finder.search_current("BH04", QUESTION)[0]["text"])

    def test_collision_rolls_back_new_projects_and_preserves_published_index(self):
        self.add_existing_document("BH04")
        control.publish(self.db)
        before = self.index.read_bytes()
        with self.assertRaisesRegex(ValueError, "BH04"):
            library.import_library(self.db)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(self.db.execute("SELECT count(*) FROM project_names").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM versions").fetchone()[0], 1)

    def test_employee_sees_names_and_originals_only_for_assigned_projects(self):
        library.import_library(self.db)
        control.publish(self.db)
        with self.db:
            accounts.add(self.db, "roads-employee", "fixture-password-only", "employee", ["BH04"])
        with TestClient(app) as client:
            api = "/workbench/api/"
            signed_in = client.post(api + "login", headers={"x-wb-login": "1"},
                                    json={"login": "roads-employee", "password": "fixture-password-only"})
            self.assertEqual(signed_in.status_code, 200)
            user = client.get(api + "me").json()
            self.assertEqual(user["project_names"], {"BH04": "滨河路综合改造"})
            client.headers["x-wb-csrf"] = user["csrf"]
            versions = client.get(api + "BH04/versions").json()
            self.assertEqual(len(versions), 6)
            self.assertTrue(all(row["status"] == "effective" for row in versions))
            result = client.post(api + "BH04/ask", json={"question": QUESTION}).json()
            self.assertEqual(result["results"][0]["chunk_id"], "BH04/BH-DC-101/R02/2.1")
            self.assertEqual(client.get(api + "XR03/versions").status_code, 404)
            self.assertEqual(client.get(api + "BH04/BH-DC-101/R01/original").status_code, 404)
            self.assertEqual(client.get(api + "BH04/BH-RC-307/R01/original").content,
                             (library.LIBRARY / "BH04/BH-RC-307_R01.pdf").read_bytes())


if __name__ == "__main__":
    unittest.main()
