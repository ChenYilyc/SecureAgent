"""Browser workflow for the same register used by document_control."""
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app import document_control as control
from app import workbench_accounts as accounts
from app.answer_construction import choose_by_part, choose_clause
from app.office_sources import MAX_BYTES, source_location
from app.search_construction import search_current
from app.semantic_retrieval import RetrievalUnavailable

router = APIRouter(prefix="/workbench", include_in_schema=False)
ASSETS = Path(__file__).with_name("workbench_static")
COOKIE = "sa_workbench_session"
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
           "Referrer-Policy": "no-referrer"}
MEDIA = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".pdf": "application/pdf", ".md": "text/plain; charset=utf-8"}


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=1, max_length=48)
    password: str = Field(min_length=1, max_length=256)


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_current: str | None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EmployeeQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)


class Question(EmployeeQuestion):
    use_model: bool = False
    multi: bool = False


def principal(req, project=None, roles=None):
    with closing(accounts.connect()) as db:
        identity = accounts.session(db, req.cookies.get(COOKIE))
    if not identity:
        raise HTTPException(401, "请先登录。")
    if project is not None and project not in identity["projects"]:
        raise HTTPException(404, "项目或资料不存在。")
    if roles and identity["role"] not in roles:
        raise HTTPException(403, "此操作需要相应的岗位权限。")
    if req.method != "GET":
        supplied = req.headers.get("x-wb-csrf", "").encode("utf-8")
        if not hmac.compare_digest(identity["csrf"].encode("ascii"), supplied):
            raise HTTPException(403, "页面会话已失效，请刷新。")
    return identity


def version(db, identity, key):
    row = db.execute("SELECT * FROM versions WHERE project_id=? AND document_id=? AND revision=?", key).fetchone()
    if not row or (identity["role"] == "employee" and row["status"] != "effective"):
        raise HTTPException(404, "资料不可用。")
    return row


def reference(row):
    key = [row["project_id"], row["document_id"], row["revision"]]
    section = row.get("fragment_id", row["section"])
    return {**row, "location": source_location(row), "preview_url": "/workbench?" + urlencode(
        dict(zip(("project", "document", "revision", "section"), [*key, section]))
    )}


@router.get("")
def page():
    return FileResponse(ASSETS / "index.html", headers={**HEADERS, "Content-Security-Policy":
        "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})


@router.get("/ui.js")
def script():
    return FileResponse(ASSETS / "ui.js", media_type="text/javascript", headers=HEADERS)


@router.get("/ui.css")
def stylesheet():
    return FileResponse(ASSETS / "ui.css", media_type="text/css", headers=HEADERS)


@router.post("/api/login")
def login(form: Login, request: Request):
    if request.headers.get("x-wb-login") != "1":
        raise HTTPException(403, "请从工作台登录。")
    with closing(accounts.connect()) as db:
        token = accounts.sign_in(db, form.login, form.password)
    if not token:
        raise HTTPException(401, "账号或口令错误。")
    response = JSONResponse({"ok": True}, headers=HEADERS)
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/workbench",
                        secure=request.url.scheme == "https", max_age=accounts.HOURS*3600)
    return response


@router.get("/api/me")
def me(request: Request):
    user = principal(request)
    allowed = remote_qa_projects()
    qa_projects = [project for project in user["projects"] if project in allowed]
    projects = user["projects"]
    placeholders = ",".join("?" for _ in projects)
    with closing(accounts.connect()) as db:
        names = dict(db.execute(
            f"SELECT project_id, display_name FROM project_names WHERE project_id IN ({placeholders})",
            projects,
        ))
    return JSONResponse({**user, "role_label": accounts.ROLES[user["role"]],
                         "remote_enabled": bool(qa_projects), "qa_projects": qa_projects,
                         "project_names": names}, headers=HEADERS)


@router.post("/api/logout")
def logout(request: Request):
    principal(request)
    with closing(accounts.connect()) as db, db:
        db.execute("DELETE FROM wb_sessions WHERE token_hash=?", (accounts.fingerprint(request.cookies[COOKIE]),))
    response = JSONResponse({"ok": True}, headers=HEADERS)
    response.delete_cookie(COOKIE, path="/workbench")
    return response


@router.get("/api/{project}/versions")
def versions(project: str, request: Request):
    user = principal(request, project)
    with closing(accounts.connect()) as db:
        rows = db.execute("""SELECT document_id,revision,status,title FROM versions WHERE project_id=?
                          AND (? != 'employee' OR status='effective')
                          ORDER BY CASE status WHEN 'draft' THEN 0 WHEN 'effective' THEN 1 ELSE 2 END,document_id,revision""",
                          (project, user["role"])).fetchall()
    return JSONResponse([dict(r) for r in rows], headers=HEADERS)


def register_upload(key, data, ext, actor):
    try:
        with closing(accounts.connect()) as db, control.transaction(db):
            return control.record_version(db, key, data, "draft", actor, extension=ext)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/api/{project}/upload")
async def upload(project: str, document: str, revision: str, format: str, request: Request):
    user = principal(request, project, {"clerk"})
    extension = "." + format.lower()
    if (extension not in MEDIA or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,70}", document)
            or not re.fullmatch(r"R[0-9]{1,8}", revision)):
        raise HTTPException(422, "请检查文号、修订号（如 R01）及文件类型。")
    raw = bytearray()
    async for part in request.stream():
        if len(raw) + len(part) > MAX_BYTES:
            raise HTTPException(413, "单份文件限 8 MiB。")
        raw.extend(part)
    if not raw:
        raise HTTPException(422, "请选择有内容的文件。")
    created = await run_in_threadpool(register_upload, (project, document, revision), bytes(raw),
                                      extension, user["login"])
    return {"created": created, "document_id": document, "revision": revision}


@router.get("/api/{project}/{document}/{rev}/preview")
def preview(project: str, document: str, rev: str, request: Request):
    user = principal(request, project)
    with closing(accounts.connect()) as db:
        row = version(db, user, (project, document, rev))
        current = db.execute("SELECT revision FROM versions WHERE project_id=? AND document_id=? "
                             "AND status='effective'", (project, document)).fetchone()
        events = db.execute("SELECT created_utc,action,actor FROM events WHERE project_id=? AND document_id=? "
                            "AND revision=? ORDER BY id", (project, document, rev)).fetchall()
    parts = json.loads(row["chunks"])
    return JSONResponse({"title": row["title"], "status": row["status"], "sha256": row["sha256"],
                         "current": current[0] if current else None,
                         "extension": Path(parts[0]["source"]).suffix,
                         "sections": [{**part, "location": source_location(part)} for part in parts],
                         "events": [dict(e) for e in events] if user["role"] != "employee" else []},
                        headers=HEADERS)


@router.post("/api/{project}/{document}/{rev}/approve")
def approval(project: str, document: str, rev: str, body: Approval, request: Request):
    user = principal(request, project, {"reviewer"})
    key = (project, document, rev)
    with closing(accounts.connect()) as db:
        row = version(db, user, key)
        if row["sha256"] != body.sha256:
            raise HTTPException(409, "与预览的原件不同，请刷新。")
        try:
            control.approve(db, key, user["login"], expected_current=body.expected_current)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        try:
            count = control.publish(db)
        except OSError:
            raise HTTPException(503, "审批已记账，索引发布失败。请重新发布索引。") from None
    return {"status": "effective", "indexed_chunks": count}


@router.get("/api/{project}/{document}/{rev}/original")
def original(project: str, document: str, rev: str, request: Request):
    user = principal(request, project)
    with closing(accounts.connect()) as db:
        row = version(db, user, (project, document, rev))
    suffix = Path(json.loads(row["chunks"])[0]["source"]).suffix
    if suffix not in MEDIA or not re.fullmatch("[a-f0-9]{64}", row["sha256"]):
        raise HTTPException(404, "无可用原件。")
    path = control.ARCHIVE / (row["sha256"] + suffix)
    if not path.is_file():
        raise HTTPException(404, "归档原件缺失。")
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != row["sha256"]:
        raise HTTPException(409, "归档原件未通过完整性检查。")
    disposition = "attachment" if suffix == ".docx" else "inline"
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{document}_{rev}{suffix}")
    return Response(blob, media_type=MEDIA[suffix],
                    headers={**HEADERS, "Content-Disposition": f'{disposition}; filename="{name}"'})


def remote_qa_projects() -> set[str]:
    if os.getenv("SECUREAGENT_ALLOW_REMOTE_QA") != "1" or not os.getenv("DEEPSEEK_API_KEY", "").strip():
        return set()
    configured = os.getenv("SECUREAGENT_QA_PROJECTS", "")
    return {project.strip() for project in configured.split(",") if project.strip()}


def current_only(db, project, hits):
    active = {(r[0], r[1]) for r in db.execute(
        "SELECT document_id,revision FROM versions WHERE project_id=? AND status='effective'", (project,))}
    return [r for r in hits if r["project_id"] == project and (r["document_id"], r["revision"]) in active]


@router.post("/api/{project}/ask")
def ask(project: str, query: EmployeeQuestion, request: Request):
    principal(request, project)
    use_model = project in remote_qa_projects()
    result = run_query(project, query.question, use_model=use_model, multi=use_model)
    return JSONResponse({"mode": "answer" if use_model else "search", **result}, headers=HEADERS)


@router.post("/api/{project}/search")
def search(project: str, query: Question, request: Request):
    principal(request, project)
    if query.multi and not query.use_model:
        raise HTTPException(422, "逐项核对需要启用模型。")
    if query.use_model and project not in remote_qa_projects():
        raise HTTPException(409, "此项目尚未启用外部问答。")
    result = run_query(project, query.question, use_model=query.use_model, multi=query.multi)
    return JSONResponse(result, headers=HEADERS)


def run_query(project: str, question: str, *, use_model: bool, multi: bool) -> dict:
    try:
        hits = search_current(project, question)
    except RetrievalUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except FileNotFoundError:
        hits = []
    with closing(accounts.connect()) as db:
        hits = current_only(db, project, hits)
    selected = None
    parts: list[tuple[str, list[str]]] = []
    if use_model:
        evidence = {r["chunk_id"]: r["text"] for r in hits}
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        try:
            if multi:
                parts = choose_by_part(question, evidence, api_key=api_key)
                if not parts:
                    raise HTTPException(422, "请输入含有文字或数字的问题。")
            elif evidence:
                selected = choose_clause(question, evidence, api_key=api_key)
        except SystemExit:
            raise HTTPException(502, "模型未能完成条款核对，请稍后重试。") from None
        if selected is not None and selected not in evidence:
            raise HTTPException(502, "模型引用未通过候选条款校验。")
    with closing(accounts.connect()) as db:
        hits = current_only(db, project, hits)
    if multi:
        current = {r["chunk_id"]: reference(r) for r in hits}
        answers = []
        for part, ids in parts:
            # A new revision may have replaced one of the jointly required citations.
            citations = [current[cid] for cid in ids] if all(cid in current for cid in ids) else []
            answers.append({"question": part, "citations": citations})
        return {"answers": answers}
    if selected not in {r["chunk_id"] for r in hits}:
        selected = None
    return {"results": [reference(r) for r in hits], "selected": selected}
