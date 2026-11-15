"""Manage projects, company accounts and per-project roles in PostgreSQL."""
from collections.abc import Iterator
from contextlib import closing, contextmanager
import os
import secrets
import sqlite3
from typing import Annotated, Literal

from fastapi import HTTPException
import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app import document_control as control
from app import workbench_accounts as accounts
from app import workbench_auth as auth


ProjectId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
ProjectRole = Literal["clerk", "reviewer", "employee"]
LoginName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,48}$")]


class NewProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: ProjectId
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class NewUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: LoginName
    password: str = Field(min_length=12, max_length=256, repr=False, exclude=True)
    project_roles: dict[ProjectId, ProjectRole] = Field(default_factory=dict)
    project_id: ProjectId | None = None
    role: ProjectRole | None = None

    @model_validator(mode="after")
    def normalize_grants(self):
        if "project_roles" in self.model_fields_set:
            if not self.project_roles:
                raise ValueError("新用户至少需要一个工程岗位。")
            if {"project_id", "role"} & self.model_fields_set:
                raise ValueError("请选择一种工程岗位提交格式。")
        else:
            if self.project_id is None or self.role is None:
                raise ValueError("新用户至少需要一个工程岗位。")
            self.project_roles = {self.project_id: self.role}
        return self


class MembershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: LoginName
    expected_revision: int = Field(ge=1, strict=True)
    project_roles: dict[ProjectId, ProjectRole]


@contextmanager
def _admin_database(actor: str) -> Iterator[psycopg.Connection]:
    dsn = os.environ.get("SECUREAGENT_DATABASE_URL")
    if not dsn:
        raise HTTPException(503, "工作台的 PostgreSQL 连接尚未配置。")
    with auth.postgres_connection(dsn) as db:
        # Keep the grant valid through the write and its audit record.
        granted = db.execute(
            """SELECT 1 FROM secureagent.wb_accounts
               WHERE login = %s AND is_company_admin AND is_active FOR SHARE""",
            (actor,),
        ).fetchone()
        if granted is None:
            raise HTTPException(403, "此操作需要公司管理员权限。")
        yield db


def _legacy_project_exists(project_id: str) -> bool:
    # Until documents move to PostgreSQL, their lookup still uses the local project code.
    if not control.REGISTER.is_file():
        return False
    uri = control.REGISTER.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as registry:
            tables = {name for (name,) in registry.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            for table in ("project_names", "versions"):
                if table in tables and registry.execute(
                    f"SELECT 1 FROM {table} WHERE project_id = ? LIMIT 1", (project_id,)
                ).fetchone():
                    return True
    except sqlite3.Error:
        raise HTTPException(503, "暂时无法核对本地文控台账的项目编号。") from None
    return False


def directory(actor: str) -> dict:
    with _admin_database(actor) as db:
        projects = db.execute(
            """SELECT project_id, display_name, state, lifecycle_revision FROM secureagent.projects
               WHERE state <> 'deleted' ORDER BY project_id"""
        ).fetchall()
        users = db.execute(
            """SELECT a.login, a.is_active, a.is_company_admin, a.access_revision,
                      COALESCE((SELECT jsonb_object_agg(m.project_id, m.role)
                                FROM secureagent.project_members AS m
                                JOIN secureagent.projects AS p USING (project_id)
                                WHERE m.login = a.login AND p.state <> 'deleted'),
                               '{}'::jsonb) AS project_roles
               FROM secureagent.wb_accounts AS a WHERE a.deleted_at IS NULL ORDER BY a.login"""
        ).fetchall()
    return {"projects": projects, "users": users, "roles": accounts.ROLES}


def create_project(actor: str, form: NewProject, request_id: str) -> dict:
    with _admin_database(actor) as db:
        project = db.execute(
            """INSERT INTO secureagent.projects (project_id, display_name) VALUES (%s, %s)
               ON CONFLICT (project_id) DO NOTHING RETURNING project_id, display_name""",
            (form.project_id, form.display_name),
        ).fetchone()
        if project is None:
            raise HTTPException(409, "该工程编号已使用，请选择现有工程或使用新编号。")
        if not auth.postgres_documents() and _legacy_project_exists(form.project_id):
            raise HTTPException(409, "该编号已用于本地文控台账中的现有工程，请为新工程选用其他编号。")
        db.execute(
            """INSERT INTO secureagent.admin_events (request_id, actor_login, action, project_id)
               VALUES (%s, %s, 'project_created', %s)""",
            (request_id, actor, form.project_id),
        )
    return project


def _revoke_project_members(db, project_id: str) -> None:
    # Membership editors lock projects before accounts. Lock affected accounts in login order.
    db.execute(
        """SELECT a.login FROM secureagent.wb_accounts AS a
           JOIN secureagent.project_members AS m ON m.login = a.login
           WHERE m.project_id = %s AND NOT a.is_company_admin
           ORDER BY a.login FOR UPDATE OF a""",
        (project_id,),
    ).fetchall()
    db.execute(
        """WITH removed AS (
               DELETE FROM secureagent.project_members WHERE project_id = %s RETURNING login
           )
           UPDATE secureagent.wb_accounts SET access_revision = access_revision + 1
           WHERE NOT is_company_admin AND login IN (SELECT login FROM removed)""",
        (project_id,),
    )


def change_project_state(actor: str, project_id: str, target: Literal["archived", "deleted"],
                         expected_revision: int, request_id: str) -> dict:
    with _admin_database(actor) as db:
        row = db.execute(
            """SELECT project_id, state, lifecycle_revision FROM secureagent.projects
               WHERE project_id = %s FOR UPDATE""", (project_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "工程不存在。")
        if row["state"] == target:
            return row
        if row["state"] == "deleted":
            raise HTTPException(404, "工程不存在。")
        if row["lifecycle_revision"] != expected_revision:
            raise HTTPException(409, "工程状态已变化，请刷新后重新确认。")
        if target == "deleted":
            _revoke_project_members(db, project_id)
        row = db.execute(
            """UPDATE secureagent.projects SET state = %s, lifecycle_revision = lifecycle_revision + 1
               WHERE project_id = %s RETURNING project_id, state, lifecycle_revision""",
            (target, project_id),
        ).fetchone()
        db.execute(
            """INSERT INTO secureagent.admin_events (request_id, actor_login, action, project_id)
               VALUES (%s, %s, %s, %s)""", (request_id, actor, "project_" + target, project_id),
        )
    return row


def _lock_projects(db, roles: dict[str, str]) -> None:
    if not roles:
        return
    found = db.execute(
        """SELECT project_id FROM secureagent.projects
           WHERE project_id = ANY(%s::text[]) AND state <> 'deleted' ORDER BY project_id FOR SHARE""",
        (sorted(roles),),
    ).fetchall()
    if len(found) != len(roles):
        raise HTTPException(404, "有工程不存在，请刷新项目列表。")


def _lock_business_account(db, login: str) -> dict:
    # Admins are outside business-account lifecycle operations. Avoid locking a second admin.
    account = db.execute(
        """SELECT access_revision, deleted_at FROM secureagent.wb_accounts
           WHERE login = %s AND NOT is_company_admin FOR UPDATE""",
        (login,),
    ).fetchone()
    if account is not None:
        return account
    if db.execute(
        "SELECT 1 FROM secureagent.wb_accounts WHERE login = %s AND is_company_admin", (login,)
    ).fetchone():
        raise HTTPException(403, "公司管理员账号受保护。")
    raise HTTPException(404, "用户不存在。")


def _current_roles(db, login: str) -> dict[str, str]:
    rows = db.execute(
        """SELECT m.project_id, m.role FROM secureagent.project_members AS m
           JOIN secureagent.projects AS p USING (project_id)
           WHERE m.login = %s AND p.state <> 'deleted'""", (login,)
    ).fetchall()
    return {row["project_id"]: row["role"] for row in rows}


def _write_roles(db, login: str, before: dict[str, str], after: dict[str, str]) -> None:
    removed = sorted(before.keys() - after.keys())
    if removed:
        db.execute(
            "DELETE FROM secureagent.project_members WHERE login = %s AND project_id = ANY(%s::text[])",
            (login, removed),
        )
    changed = [(login, code, role) for code, role in sorted(after.items()) if before.get(code) != role]
    for grant in changed:
        db.execute(
            """INSERT INTO secureagent.project_members (login, project_id, role) VALUES (%s, %s, %s)
               ON CONFLICT (login, project_id) DO UPDATE SET role = EXCLUDED.role""", grant,
        )


def _audit_roles(db, request_id: str, actor: str, action: str, login: str,
                 before: dict[str, str] | None, after: dict[str, str]) -> None:
    code = role = None
    if action == "user_created" and len(after) == 1:
        code, role = next(iter(after.items()))
    db.execute(
        """INSERT INTO secureagent.admin_events
              (request_id, actor_login, action, target_login, project_id, project_role, roles_before, roles_after)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (request_id, actor, action, login, code, role, Jsonb(before) if before is not None else None, Jsonb(after)),
    )


def create_user(actor: str, form: NewUser, request_id: str) -> dict:
    roles = form.project_roles
    with _admin_database(actor) as db:
        _lock_projects(db, roles)
        salt = secrets.token_bytes(16)
        digest = accounts.password_digest(form.password, salt)
        account = db.execute(
            """INSERT INTO secureagent.wb_accounts (login, salt, digest, is_company_admin, is_active)
               VALUES (%s, %s, %s, FALSE, TRUE)
               ON CONFLICT (login) DO NOTHING RETURNING login""",
            (form.login, salt, digest),
        ).fetchone()
        if account is None:
            raise HTTPException(409, "该用户名已使用或已注销。")
        _write_roles(db, form.login, {}, roles)
        _audit_roles(db, request_id, actor, "user_created", form.login, None, roles)
    if form.project_id is not None:
        return {"login": form.login, "project_id": form.project_id, "role": form.role}
    return {"login": form.login, "project_roles": roles}


def update_memberships(actor: str, form: MembershipUpdate, request_id: str) -> dict:
    with _admin_database(actor) as db:
        _lock_projects(db, form.project_roles)
        target = _lock_business_account(db, form.login)
        if target["deleted_at"] is not None:
            raise HTTPException(404, "用户已注销。")
        before = _current_roles(db, form.login)
        revision = target["access_revision"]
        if before != form.project_roles:
            if revision != form.expected_revision:
                raise HTTPException(409, "岗位已被其他操作修改，请刷新用户列表后重试。")
            _write_roles(db, form.login, before, form.project_roles)
            revision = db.execute(
                """UPDATE secureagent.wb_accounts SET access_revision = access_revision + 1
                   WHERE login = %s RETURNING access_revision""", (form.login,),
            ).fetchone()["access_revision"]
            _audit_roles(db, request_id, actor, "memberships_changed", form.login, before, form.project_roles)
    return {"login": form.login, "project_roles": form.project_roles, "access_revision": revision}


def delete_user(actor: str, login: str, expected_revision: int, request_id: str) -> dict:
    with _admin_database(actor) as db:
        target = _lock_business_account(db, login)
        if target["deleted_at"] is None:
            if target["access_revision"] != expected_revision:
                raise HTTPException(409, "账号的工程岗位已变更，请刷新用户列表后重新确认。")
            before = _current_roles(db, login)
            db.execute("DELETE FROM secureagent.wb_sessions WHERE login = %s", (login,))
            db.execute("DELETE FROM secureagent.project_members WHERE login = %s", (login,))
            db.execute(
                """UPDATE secureagent.wb_accounts
                   SET deleted_at = CURRENT_TIMESTAMP, is_active = FALSE, salt = %s, digest = %s,
                       access_revision = access_revision + 1
                   WHERE login = %s""", (b"", b"", login),
            )
            _audit_roles(db, request_id, actor, "user_deleted", login, before, {})
    return {"login": login, "deleted": True}
