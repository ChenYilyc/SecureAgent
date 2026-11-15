import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from app import answer_construction as qa


FILE = "QL01/QL-DOC-007/R01/3.2"
KEEP = "QL01/QL-ARC-014/R01/4.2"
QUESTION = "会审纪要由谁归档？归档后，原件由谁长期保管？"


def row(key, text):
    project, doc, rev, section = key.split("/")
    return {"chunk_id": key, "project_id": project, "status": "effective", "document_id": doc,
            "revision": rev, "section": section, "text": text, "source": doc + ".md", "source_line": 3}


class MultiEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(KEEP, "原件由项目档案管理员长期保管。"), row(FILE, "由项目资料员归档。")]
        self.evidence = {r["chunk_id"]: r["text"] for r in self.rows}
        self.post = self.enterContext(patch.object(qa.httpx2, "post"))
        self.search = self.enterContext(patch.object(qa, "search_current", return_value=self.rows))
        self.enterContext(patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-placeholder"}))

    def model_returns(self, content):
        response = Mock()
        response.json.return_value = {"choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(content)}}
        ]}
        self.post.return_value = response

    def invoke(self, question=QUESTION, *, multi=True):
        args = ["answer_construction", "--project", "QL01"]
        if multi:
            args.append("--multi")
        args.append(question)
        output = io.StringIO()
        with patch.object(sys, "argv", args), redirect_stdout(output):
            qa.main()
        return output.getvalue()

    def test_each_fact_prints_its_own_original_and_reference(self):
        self.model_returns({"citations": {"2": [KEEP], "1": [FILE]}})
        text = self.invoke()
        filing, custody = text.split("子问 1")[1].split("子问 2")
        self.assertIn(self.evidence[FILE], filing)
        self.assertIn("QL-DOC-007 · R01 · §3.2", filing)
        self.assertNotIn(self.evidence[KEEP], filing)
        self.assertIn(self.evidence[KEEP], custody)
        self.assertIn("QL-ARC-014.md:3", custody)
        self.search.assert_called_once_with("QL01", QUESTION)
        self.post.assert_called_once()
        payload = json.loads(self.post.call_args.kwargs["json"]["messages"][1]["content"])
        self.assertEqual(payload, {"question": QUESTION, "parts": {"1": "会审纪要由谁归档？",
                         "2": "归档后，原件由谁长期保管？"}, "evidence": self.evidence})

    def test_missing_evidence_abstains_on_that_part(self):
        self.model_returns({"citations": {"1": [FILE], "2": []}})
        text = self.invoke("会审纪要由谁归档？延期罚多少钱？")
        self.assertIn(self.evidence[FILE], text)
        self.assertIn("依据不足", text.split("子问 2")[1])
        self.assertNotIn("答复（条款原文）", text.split("子问 2")[1])

    def test_single_group_can_cite_two_passages(self):
        self.model_returns({"citations": {"1": [FILE, KEEP]}})
        text = self.invoke("请给出归档与长期保管的职责")
        self.assertEqual(text.count("答复（条款原文）"), 2)

    def test_incomplete_duplicate_and_outside_ids_fail_before_any_answer_is_displayed(self):
        for groups in ({"1": [FILE]}, {"1": [FILE], "2": ["HT02/other"]},
                       {"1": [FILE, FILE], "2": [KEEP]}, {"1": [FILE], "2": [KEEP], "3": []}):
            with self.subTest(groups=groups):
                self.model_returns({"citations": groups})
                with patch.object(qa, "print_clause") as display, self.assertRaises(SystemExit):
                    self.invoke()
                display.assert_not_called()

    def test_no_candidates_skips_provider_and_single_choice_keeps_its_contract(self):
        self.search.return_value = []
        self.assertIn("未检索到", self.invoke())
        self.post.assert_not_called()
        self.search.return_value = self.rows
        self.model_returns({"chunk_id": FILE})
        self.assertIn("QL-DOC-007 · R01 · §3.2", self.invoke("会审纪要由谁归档？", multi=False))
        self.assertEqual(self.post.call_args.kwargs["json"]["max_tokens"], 128)
