"""Browser workflow for the same register used by document_control."""
from contextlib import closing
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
from typing import Annotated, TypeVar
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app import document_control as control
from app import postgres_documents as pg
from app import workbench_accounts as accounts
from app import workbench_admin as admin
from app import workbench_auth as auth
from app.answer_construction import ModelServiceUnavailable, choose_by_part, choose_clause
from app.office_sources import MAX_BYTES, source_location
from app.search_construction import rank_current, search_current
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


def upload_limit() -> int:
    return 4 * 1024 * 1024 if os.getenv("VERCEL") == "1" else MAX_BYTES


def streamed(blob: bytes, media_type: str, headers: dict) -> StreamingResponse:
    chunks = (blob[offset:offset + 65536] for offset in range(0, len(blob), 65536))
    return StreamingResponse(chunks, media_type=media_type, headers=headers)


def document_json(value) -> Response:
    response = JSONResponse(value, headers=HEADERS)
    if os.getenv("VERCEL") == "1" and len(response.body) > upload_limit():
        return streamed(response.body, "application/json", HEADERS)
    return response


def principal(req, project=None, roles=None, *, document_write=False):
    identity = auth.session(req.cookies.get(COOKIE))
    if not identity:
        raise HTTPException(401, "请先登录。")
    if project is not None:
        project_role = identity["project_roles"].get(project)
        if project_role is None:
            raise HTTPException(404, "项目或资料不存在。")
        identity = {**identity, "role": project_role}
    if roles and identity["role"] not in roles:
        raise HTTPException(403, "此操作需要相应的岗位权限。")
    if req.method != "GET":
        supplied = req.headers.get("x-wb-csrf", "").encode("utf-8")
        if not hmac.compare_digest(identity["csrf"].encode("ascii"), supplied):
            raise HTTPException(403, "页面会话已失效，请刷新。")
    if document_write and identity["project_states"][project] == "archived":
        raise HTTPException(409, "工程已归档，文档只读。")
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
    token = auth.sign_in(form.login, form.password)
    if not token:
        raise HTTPException(401, "账号或口令错误。")
    response = JSONResponse({"ok": True}, headers=HEADERS)
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/workbench",
                        secure=os.getenv("VERCEL") == "1" or request.url.scheme == "https", max_age=accounts.HOURS*3600)
    return response


@router.get("/api/me")
def me(request: Request):
    user = principal(request)
    allowed = remote_qa_projects()
    qa_projects = [project for project in user["projects"] if project in allowed]
    role_label = "公司管理员" if user["is_company_admin"] else accounts.ROLES.get(user["role"], "项目成员")
    return JSONResponse({**user, "role_label": role_label, "remote_enabled": bool(qa_projects),
                         "qa_projects": qa_projects, "upload_limit": upload_limit()}, headers=HEADERS)


@router.post("/api/logout")
def logout(request: Request):
    principal(request)
    auth.sign_out(request.cookies[COOKIE])
    response = JSONResponse({"ok": True}, headers=HEADERS)
    response.delete_cookie(COOKIE, path="/workbench")
    return response


def company_admin(request: Request) -> str:
    user = principal(request)
    if not user["is_company_admin"]:
        raise HTTPException(403, "此操作需要公司管理员权限。")
    return user["login"]


AdminLogin = Annotated[str, Depends(company_admin)]
AdminForm = TypeVar("AdminForm", bound=BaseModel)


async def admin_form(request: Request, model: type[AdminForm], hint: str) -> AdminForm:
    try:
        return model.model_validate_json(await request.body())
    except ValidationError:
        # Validation errors may contain the submitted password as part of their input.
        raise HTTPException(422, hint) from None


@router.get("/api/admin/users")
def admin_users(actor: AdminLogin):
    return JSONResponse(admin.directory(actor), headers=HEADERS)


@router.post("/api/admin/projects")
async def admin_create_project(request: Request, actor: AdminLogin):
    form = await admin_form(request, admin.NewProject, "请核对工程编号和名称。")
    result = await run_in_threadpool(admin.create_project, actor, form, request.state.request_id)
    return JSONResponse(result, status_code=201, headers=HEADERS)


@router.post("/api/admin/projects/{project_id}/archive")
def admin_archive_project(project_id: admin.ProjectId, request: Request, actor: AdminLogin,
                          expected_revision: Annotated[int, Query(ge=1)]):
    result = admin.change_project_state(actor, project_id, "archived", expected_revision, request.state.request_id)
    return JSONResponse(result, headers=HEADERS)


@router.delete("/api/admin/projects/{project_id}")
def admin_delete_project(project_id: admin.ProjectId, request: Request, actor: AdminLogin,
                         expected_revision: Annotated[int, Query(ge=1)]):
    result = admin.change_project_state(actor, project_id, "deleted", expected_revision, request.state.request_id)
    return JSONResponse(result, headers=HEADERS)


@router.post("/api/admin/users")
async def admin_create_user(request: Request, actor: AdminLogin):
    form = await admin_form(request, admin.NewUser, "请核对用户名、密码长度，以及每个已选工程的岗位。")
    result = await run_in_threadpool(admin.create_user, actor, form, request.state.request_id)
    return JSONResponse(result, status_code=201, headers=HEADERS)


@router.put("/api/admin/users/memberships")
async def admin_update_memberships(request: Request, actor: AdminLogin):
    form = await admin_form(request, admin.MembershipUpdate, "请核对用户名、工程和项目岗位。")
    result = await run_in_threadpool(admin.update_memberships, actor, form, request.state.request_id)
    return JSONResponse(result, headers=HEADERS)


@router.delete("/api/admin/users")
def admin_delete_user(request: Request, actor: AdminLogin, login: admin.LoginName,
                      expected_revision: Annotated[int, Query(ge=1)]):
    result = admin.delete_user(actor, login, expected_revision, request.state.request_id)
    return JSONResponse(result, headers=HEADERS)


@router.get("/api/{project}/versions")
def versions(project: str, request: Request):
    user = principal(request, project)
    if auth.postgres_documents():
        return document_json(pg.list_versions(project, user["login"]))
    with closing(accounts.connect()) as db:
        rows = db.execute("""SELECT document_id,revision,status,title FROM versions WHERE project_id=?
                          AND (? != 'employee' OR status='effective')
                          ORDER BY CASE status WHEN 'draft' THEN 0 WHEN 'effective' THEN 1 ELSE 2 END,document_id,revision""",
                          (project, user["role"])).fetchall()
    return JSONResponse([dict(r) for r in rows], headers=HEADERS)


def register_upload(key, data, ext, actor):
    try:
        with auth.document_write(key[0], actor, "clerk") as postgres:
            if auth.postgres_documents():
                return pg.receive(postgres, key, data, ext, actor)
            with closing(accounts.connect()) as db, control.transaction(db):
                return control.record_version(db, key, data, "draft", actor, extension=ext)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/api/{project}/upload")
async def upload(project: str, document: str, revision: str, format: str, request: Request):
    user = principal(request, project, {"clerk"}, document_write=True)
    extension = "." + format.lower()
    if (extension not in MEDIA or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,70}", document)
            or not re.fullmatch(r"R[0-9]{1,8}", revision)):
        raise HTTPException(422, "请检查文号、修订号（如 R01）及文件类型。")
    raw = bytearray()
    limit = upload_limit()
    async for part in request.stream():
        if len(raw) + len(part) > limit:
            raise HTTPException(413, f"单份文件限 {limit // (1024 * 1024)} MiB。")
        raw.extend(part)
    if not raw:
        raise HTTPException(422, "请选择有内容的文件。")
    created = await run_in_threadpool(register_upload, (project, document, revision), bytes(raw),
                                      extension, user["login"])
    return {"created": created, "document_id": document, "revision": revision}


@router.get("/api/{project}/{document}/{rev}/preview")
def preview(project: str, document: str, rev: str, request: Request):
    user = principal(request, project)
    if auth.postgres_documents():
        row = pg.preview((project, document, rev), user["login"])
        row["sections"] = [{**part, "location": source_location(part)} for part in row.pop("chunks")]
        return document_json(row)
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
    user = principal(request, project, {"reviewer"}, document_write=True)
    key = (project, document, rev)
    if auth.postgres_documents():
        with auth.document_write(project, user["login"], "reviewer") as db:
            count = pg.approve(db, key, user["login"], body.sha256, body.expected_current)
        return {"status": "effective", "indexed_chunks": count}
    with auth.document_write(project, user["login"], "reviewer"), closing(accounts.connect()) as db:
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
    if auth.postgres_documents():
        row = pg.original((project, document, rev), user["login"])
        suffix, blob = row["extension"], bytes(row["original_bytes"])
    else:
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
    headers = {**HEADERS, "Content-Disposition": f'{disposition}; filename="{name}"'}
    if os.getenv("VERCEL") == "1" and len(blob) > upload_limit():
        return streamed(blob, MEDIA[suffix], headers)
    return Response(blob, media_type=MEDIA[suffix], headers=headers)


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
    user = principal(request, project)
    use_model = project in remote_qa_projects()
    result = run_query(project, query.question, use_model=use_model, multi=use_model,
                       login=user["login"], allow_search_fallback=True)
    principal(request, project)  # Access can change while the model is answering.
    return document_json({"mode": "answer" if "answers" in result else "search", **result})


@router.post("/api/{project}/search")
def search(project: str, query: Question, request: Request):
    user = principal(request, project)
    if query.multi and not query.use_model:
        raise HTTPException(422, "逐项核对需要启用模型。")
    if query.use_model and project not in remote_qa_projects():
        raise HTTPException(409, "此项目尚未启用外部问答。")
    result = run_query(project, query.question, use_model=query.use_model, multi=query.multi, login=user["login"])
    principal(request, project)
    return document_json(result)


def refreshed_hits(project, login, hits):
    if auth.postgres_documents():
        return pg.current_hits(project, login, hits)
    with closing(accounts.connect()) as db:
        return current_only(db, project, hits)


def run_query(project: str, question: str, *, use_model: bool, multi: bool,
              login: str | None = None, allow_search_fallback: bool = False) -> dict:
    notices = []
    try:
        if auth.postgres_documents():
            rows = pg.current_chunks(project, login)
            mode = os.getenv("SECUREAGENT_RETRIEVAL") or "hybrid"
            try:
                hits = rank_current(project, question, rows, mode=mode)
            except RetrievalUnavailable as exc:
                if mode != "hybrid":
                    raise
                logging.getLogger(__name__).warning("semantic retrieval unavailable: %s", exc)
                hits = rank_current(project, question, rows, mode="lexical")
                notices.append("语义检索暂不可用，当前按关键词匹配。")
        else:
            hits = search_current(project, question)
    except RetrievalUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except FileNotFoundError:
        hits = []
    hits = refreshed_hits(project, login, hits)
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
        except ModelServiceUnavailable:
            if not allow_search_fallback:
                raise HTTPException(502, "模型服务暂不可用。") from None
            multi = False
            notices.append("DeepSeek 暂不可用，以下为检索到的候选条款。")
        except SystemExit:
            raise HTTPException(502, "模型未能完成条款核对，请稍后重试。") from None
        if selected is not None and selected not in evidence:
            raise HTTPException(502, "模型引用未通过候选条款校验。")
    hits = refreshed_hits(project, login, hits)
    extra = {"notices": notices} if notices else {}
    if multi:
        current = {r["chunk_id"]: reference(r) for r in hits}
        answers = []
        for part, ids in parts:
            # A new revision may have replaced one of the jointly required citations.
            citations = [current[cid] for cid in ids] if all(cid in current for cid in ids) else []
            answers.append({"question": part, "citations": citations})
        return {"answers": answers, **extra}
    if selected not in {r["chunk_id"] for r in hits}:
        selected = None
    return {"results": [reference(r) for r in hits], "selected": selected, **extra}
