import unittest
from pathlib import Path
from unittest.mock import patch

from app.document_tool import DocumentAccessDenied, read_document


class DocumentPermissionTests(unittest.TestCase):
    def test_revoked_employee_access_blocks_read(self) -> None:
        with patch.object(Path, "read_text") as read_text:
            with self.assertRaises(DocumentAccessDenied):
                read_document(
                    "B",
                    employee_docs=frozenset({"A"}),
                    task_docs=frozenset({"A", "B"}),
                )

            read_text.assert_not_called()
    def test_task_scope_blocks_read_before_file_access(self) -> None:
        with patch.object(Path, "read_text") as read_text:
            with self.assertRaises(DocumentAccessDenied):
                read_document(
                    "B",
                    employee_docs=frozenset({"A", "B"}),
                    task_docs=frozenset({"A"}),
                )

            read_text.assert_not_called()