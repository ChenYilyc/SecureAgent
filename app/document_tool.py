import logging
from pathlib import Path
from uuid import uuid4

audit_log = logging.getLogger("secureagent.audit.read_document")

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
DOCUMENT_FILES = {
    "A": EXAMPLES_DIR / "A.txt",
    "B": EXAMPLES_DIR / "B.txt",
}


class DocumentAccessDenied(Exception):
    """The employee or the current task lacks permission to read the document."""


def read_document(
    document_id: str,
    *,
    employee_docs: frozenset[str],
    task_docs: frozenset[str],
    request_id: str | None = None,
) -> str:
    known_id = document_id if document_id in DOCUMENT_FILES else "unknown"
    call_id = uuid4().hex
    context = f"request_id={request_id or '-'} call_id={call_id} document={known_id}"

    if document_id not in employee_docs:
        audit_log.info("%s event=denied reason=employee_scope", context)
        raise DocumentAccessDenied(document_id)

    if document_id not in task_docs:
        audit_log.info("%s event=denied reason=task_scope", context)
        raise DocumentAccessDenied(document_id)

    audit_log.info("%s event=authorized", context)

    path = DOCUMENT_FILES.get(document_id)
    if path is None:
        audit_log.warning("%s event=read_failed reason=unknown_document", context)
        raise FileNotFoundError(document_id)

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        audit_log.warning("%s event=read_failed reason=%s", context, type(exc).__name__)
        raise

    audit_log.info("%s event=read_succeeded", context)
    return content