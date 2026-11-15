import unittest

from app.answer_construction import choose_clause
from app.search_construction import search_current


CURRENT_CLAUSE = "QL01/QL-DOC-007/R01/3.2"


class ConstructionQaEval(unittest.TestCase):
    def evidence_for(self, question: str, *, mode: str | None = None) -> dict[str, str]:
        hits = search_current("QL01", question, mode=mode)
        for row in hits:
            self.assertEqual(row["project_id"], "QL01")
            self.assertEqual(row["status"], "effective")
        return {str(row["chunk_id"]): str(row["text"]) for row in hits}

    def test_filing_owner_and_location_have_evidence(self) -> None:
        question = "图纸会审纪要由谁归档，存到哪里？"
        evidence = self.evidence_for(question)

        self.assertIn(CURRENT_CLAUSE, evidence)
        self.assertIn("由项目资料员归档", evidence[CURRENT_CLAUSE])
        self.assertIn("技术管理/图纸会审", evidence[CURRENT_CLAUSE])
        self.assertEqual(choose_clause(question, evidence), CURRENT_CLAUSE)

    def test_related_clause_does_not_establish_a_deadline(self) -> None:
        question = "图纸会审纪要必须在几天内归档？"
        evidence = self.evidence_for(question)

        self.assertIn(CURRENT_CLAUSE, evidence)
        self.assertIsNone(choose_clause(question, evidence))

    def test_draft_only_term_has_no_current_evidence(self) -> None:
        self.assertEqual(self.evidence_for("工程协同", mode="lexical"), {})
        for clause_id, text in self.evidence_for("工程协同").items():
            self.assertNotEqual(clause_id, "QL01/QL-DOC-007/R02/3.2")
            self.assertNotIn("工程协同/会审纪要", text)
        
    def test_material_submission_deadline_uses_its_own_clause(self) -> None:
        question = "材料报审记录必须在几天内归档？"
        expected = "QL01/QL-DOC-009/R01/4.1"
        evidence = self.evidence_for(question)

        self.assertIn(expected, evidence)
        self.assertIn("2 个工作日内", evidence[expected])
        self.assertEqual(choose_clause(question, evidence), expected)
