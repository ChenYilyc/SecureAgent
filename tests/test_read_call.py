import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from app.read_call import execute_read_call


class ReadCallTests(unittest.TestCase):
    def test_valid_call_passes_document_id_to_reader(self) -> None:
        reader = Mock(return_value="Sample document B")

        result = execute_read_call({"document_id": "B"}, reader=reader)

        self.assertEqual(result, "Sample document B")
        reader.assert_called_once_with("B")

    def test_call_cannot_select_its_own_task(self) -> None:
        reader = Mock()
        arguments = {"document_id": "B", "task_id": "review-b"}

        with self.assertRaises(ValidationError) as raised:
            execute_read_call(arguments, reader=reader)

        error = raised.exception.errors()[0]
        self.assertEqual(error["loc"], ("task_id",))
        self.assertEqual(error["type"], "extra_forbidden")
        reader.assert_not_called()