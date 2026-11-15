import unittest
from unittest.mock import patch

from app import search_construction
from evals import retrieval_baseline as benchmark


class RetrievalBaselineTests(unittest.TestCase):
    def test_misses_count_in_mrr_denominator(self):
        first, top_three, mrr = benchmark.score_ranks([1, 2, 3, None])
        self.assertEqual((first, top_three), (1, 3))
        self.assertAlmostEqual(mrr, (1 + 1/2 + 1/3) / 4)

    def test_benchmark_uses_fixture_independently_of_live_index(self):
        with patch.object(search_construction, "CHUNKS_PATH", None):
            _, results = benchmark.evaluate()
        self.assertTrue(any(r.candidates for r in results))
        self.assertTrue(any(r.case.expected is None for r in results))
        self.assertFalse(any(r.invalid for r in results))

    def test_wrong_project_draft_and_unknown_ids_are_reported(self):
        ids = ("HT02/HT-DOC-009/R01/4.1", "QL01/QL-DOC-007/R02/3.2", "missing")
        fake_hits = [{"chunk_id": cid} for cid in ids]
        with patch.object(benchmark, "search_current", return_value=fake_hits):
            _, results = benchmark.evaluate()
        local = next(r for r in results if r.case.project == "QL01")
        self.assertEqual(local.invalid, ids)
        self.assertIsNone(local.rank)
