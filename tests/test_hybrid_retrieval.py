from contextlib import closing
from importlib.util import find_spec
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from app import search_construction as search
from app import semantic_retrieval as semantic


def record(project: str, revision: str, status: str, text: str) -> dict:
    return {
        "project_id": project, "status": status, "chunk_id": f"{project}/doc/{revision}/1",
        "title": "资料移交", "section_title": "流程", "text": text,
    }


class HybridRetrievalTests(unittest.TestCase):
    def test_dense_candidates_follow_project_and_revision_changes(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        index = folder / "records.jsonl"
        first = record("QL01", "R01", "effective", "旧时限")
        second = record("QL01", "R02", "draft", "新时限")
        other = record("HT02", "R01", "effective", "另一个项目")

        def publish():
            index.write_text("\n".join(json.dumps(r) for r in [first, second, other]), encoding="utf-8")

        publish()
        question = "何日办妥"
        self.assertEqual(search.search_current("QL01", question, index_path=index, mode="lexical"), [])
        with patch.object(search, "dense_order", return_value=[0]) as dense:
            before = search.search_current("QL01", question, index_path=index, mode="hybrid")
            self.assertEqual(before, [first])
            self.assertEqual(dense.call_args.args[2], ["资料移交 流程 旧时限"])
            first["status"] = "superseded"
            second["status"] = "effective"
            publish()
            after = search.search_current("QL01", question, index_path=index, mode="hybrid")
            self.assertEqual(after, [second])
            self.assertEqual(dense.call_args.args[2], ["资料移交 流程 新时限"])
            dense.reset_mock()
            self.assertEqual(search.search_current("unassigned", question, index_path=index, mode="hybrid"), [])
            dense.assert_not_called()

    def test_explicit_hybrid_reports_unavailable_encoder(self):
        fixtures = Path(__file__).resolve().parents[1] / "evals" / "retrieval" / "corpus.jsonl"
        with patch.object(search, "dense_order", side_effect=semantic.RetrievalUnavailable("模型未准备")):
            with self.assertRaisesRegex(semantic.RetrievalUnavailable, "模型未准备"):
                search.search_current("QL01", "图纸", index_path=fixtures, mode="hybrid")

    @unittest.skipUnless(find_spec("numpy"), "向量缓存测试使用可选的 NumPy 依赖")
    def test_content_cache_preserves_order_and_only_encodes_missing_text(self):
        import numpy as np

        encoder = Mock(np=np)
        encoder.encode.side_effect = lambda texts: np.stack(
            [np.full(512, len(text), dtype=np.float32) for text in texts]
        )
        with closing(sqlite3.connect(":memory:")) as db:
            semantic.cached_passages(db, encoder, "QL01", ["甲", "乙乙", "甲"])
            encoder.encode.assert_called_once_with(["甲", "乙乙"])
            encoder.encode.reset_mock()
            vectors = semantic.cached_passages(db, encoder, "QL01", ["乙乙", "丙丙丙", "甲"])
            encoder.encode.assert_called_once_with(["丙丙丙"])
            np.testing.assert_array_equal(vectors[:, 0], [2, 3, 1])
            encoder.encode.reset_mock()
            semantic.cached_passages(db, encoder, "HT02", ["甲"])
            encoder.encode.assert_called_once_with(["甲"])
