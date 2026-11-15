from collections.abc import Callable
from pydantic import BaseModel, ConfigDict, Field


class ReadDocumentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document_id: str = Field(description="ID of the single document to read.")


def execute_read_call(
    arguments: object,
    *,
    reader: Callable[[str], str],
) -> str:
    args = ReadDocumentArgs.model_validate(arguments)
    return reader(args.document_id)