from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import call, patch

from evals import answer_quality as qa
from evals.retrieval_baseline import Case, Result


class AnswerQualityTests(unittest.TestCase):
    def setUp(self):
        self.clause = "QL01/DOC/R02/2.1"
        self.evidence = {self.clause: "签收后一个工作日内归档。"}
        self.positive = Result(Case("P", "QL01", "几天内归档？", self.clause),
                               (self.clause,), 1, (), self.evidence)
        self.negative = Result(Case("N", "QL01", "罚多少钱？", None),
                               (self.clause,), None, (), self.evidence)
        self.out = self.enterContext(redirect_stdout(StringIO()))
        self.err = self.enterContext(redirect_stderr(StringIO()))
        self.select = self.enterContext(patch.object(qa, "choose_clause"))

    def test_same_candidates_can_support_a_citation_and_an_abstention(self):
        self.select.side_effect = [self.clause, None]
        self.assertEqual(qa.run([self.positive, self.negative], "test"), 0)
        self.assertEqual(self.select.call_args_list, [
            call(self.positive.case.question, self.evidence, api_key="test"),
            call(self.negative.case.question, self.evidence, api_key="test"),
        ])
        self.assertIn("有依据题正确引用  1/1", self.out.getvalue())
        self.assertIn("其中有候选时正确拒答  1/1", self.out.getvalue())

    def test_false_abstention_and_unknown_citation_fail(self):
        self.select.side_effect = [None, "other"]
        self.assertEqual(qa.run([self.positive, self.negative], "test"), 1)
        self.assertIn("误拒答", self.out.getvalue())
        self.assertIn("候选外引用", self.out.getvalue())
        self.assertIn("缺依据题正确拒答  0/1", self.out.getvalue())
        self.assertEqual(qa.grade(self.negative, self.clause), (False, "缺依据仍选了条款"))

    def test_no_candidates_needs_no_model_call(self):
        empty = Result(self.negative.case, (), None, (), {})
        self.assertEqual(qa.run([empty]), 0)
        self.select.assert_not_called()
        self.assertIn("检索为空，系统拒答", self.out.getvalue())
        self.assertNotIn("其中有候选", self.out.getvalue())

    def test_out_of_scope_stops_before_any_model_call(self):
        invalid = Result(self.negative.case, ("other",), None, ("other",), {})
        self.assertEqual(qa.run([self.positive, invalid]), 2)
        self.select.assert_not_called()

    def test_provider_error_is_an_incomplete_run(self):
        self.select.side_effect = [self.clause, SystemExit("模型响应格式异常")]
        self.assertEqual(qa.run([self.positive, self.negative], "test"), 2)
        self.assertIn("完成 1/2 题", self.err.getvalue())
        self.assertNotIn("缺依据题正确拒答", self.out.getvalue())
