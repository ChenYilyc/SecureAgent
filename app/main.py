from collections.abc import Awaitable, Callable
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException, Request, Response
import logging

from app.read_call import execute_read_call
from app.workbench import router as workbench_router
from app.document_tool import DocumentAccessDenied, read_document

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(message)s",
)
app = FastAPI(title="SecureAgent")
app.include_router(workbench_router)

DEMO_EMPLOYEE_ID = "employee-demo"
DEMO_EMPLOYEE_DOCS = frozenset({"A", "B"})

# (员工 ID, 任务 ID) -> 这份任务获准读取的文档
DEMO_TASK_GRANTS: dict[tuple[str, str], frozenset[str]] = {
    (DEMO_EMPLOYEE_ID, "summary-a"): frozenset({"A"}),
    (DEMO_EMPLOYEE_ID, "review-b"): frozenset({"B"}),
}

task_audit = logging.getLogger("secureagent.audit.task")


def resolve_task_docs(
    request: Request, task_id: str = "summary-a"
) -> frozenset[str]:
    docs = DEMO_TASK_GRANTS.get((DEMO_EMPLOYEE_ID, task_id))
    if docs is None:
        raise HTTPException(status_code=404, detail="Task not found")

    task_audit.info(
        "request_id=%s task_id=%s event=task_selected",
        request.state.request_id,
        task_id,
    )
    return docs


TaskDocs = Annotated[frozenset[str], Depends(resolve_task_docs)]
def bind_document_reader(
    request: Request,
    task_docs: TaskDocs,
) -> Callable[[str], str]:
    request_id = request.state.request_id

    def read_for_task(document_id: str) -> str:
        return read_document(
            document_id,
            employee_docs=DEMO_EMPLOYEE_DOCS,
            task_docs=task_docs,
            request_id=request_id,
        )

    return read_for_task


BoundReader = Annotated[Callable[[str], str], Depends(bind_document_reader)]


class ReadPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(min_length=1, max_length=5)

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



@app.get("/demo/documents/{document_id}")
def get_demo_document(document_id: str, reader: BoundReader) -> dict[str, str]:
    try:
        content = reader(document_id)
    except DocumentAccessDenied:
        raise HTTPException(status_code=403, detail="Document access denied") from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document unavailable") from None

    return {"document_id": document_id, "content": content}

@app.post("/demo/runs/read-documents")
def run_document_reads(
    reader: BoundReader,
    plan: ReadPlan | None = None,
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    document_ids = plan.document_ids if plan is not None else ["B", "A"]

    for document_id in document_ids:
        try:
            content = execute_read_call({"document_id": document_id}, reader=reader)
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
