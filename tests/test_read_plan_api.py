import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class ReadPlanApiTests(unittest.TestCase):
    def test_extra_task_scope_rejects_plan_before_any_read(self) -> None:
        plan = {"document_ids": ["A", "B"], "task_docs": ["A", "B"]}

        with TestClient(app) as client:
            with patch("app.main.read_document") as mock_read:
                response = client.post("/demo/runs/read-documents", json=plan)

        self.assertEqual(response.status_code, 422)
        error = response.json()["detail"][0]
        self.assertEqual(error["loc"], ["body", "task_docs"])
        self.assertEqual(error["type"], "extra_forbidden")
        mock_read.assert_not_called()