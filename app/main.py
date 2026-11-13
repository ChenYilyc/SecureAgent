from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
import logging

from app.document_tool import DocumentAccessDenied, read_document

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(message)s",
)
app = FastAPI(title="SecureAgent")
@app.middleware("http")
async def add_request_id(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

DEMO_EMPLOYEE_DOCS = frozenset({"A", "B"})
DEMO_TASK_DOCS = frozenset({"A"})


@app.get("/demo/documents/{document_id}")
def get_demo_document(document_id: str, request: Request) -> dict[str, str]:
    try:
        content = read_document(
            document_id,
            employee_docs=DEMO_EMPLOYEE_DOCS,
            task_docs=DEMO_TASK_DOCS,
            request_id=request.state.request_id,
        )
    except DocumentAccessDenied:
        raise HTTPException(status_code=403, detail="Document access denied") from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document unavailable") from None

    return {"document_id": document_id, "content": content}

@app.post("/demo/runs/read-documents")
def run_document_reads(request: Request) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []

    for document_id in ("B", "A"):
        try:
            content = read_document(
                document_id,
                employee_docs=DEMO_EMPLOYEE_DOCS,
                task_docs=DEMO_TASK_DOCS,
                request_id=request.state.request_id,
            )
        except DocumentAccessDenied:
            results.append({"document_id": document_id, "status": "denied"})
        except (OSError, UnicodeError):
            results.append({"document_id": document_id, "status": "unavailable"})
        else:
            results.append({
                "document_id": document_id,
                "status": "read",
                "content": content,
            })

    return results