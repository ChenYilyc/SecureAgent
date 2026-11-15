"""Issue and validate server-side sessions for the document workbench."""
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import timedelta
import hmac
import logging
import os
import secrets

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row

from app import workbench_accounts as accounts


def postgres_documents() -> bool:
    hosted = os.getenv("VERCEL") == "1"
    backend = os.getenv("SECUREAGENT_DOCUMENT_BACKEND") or ("postgres" if hosted else "sqlite")
    if backend not in {"sqlite", "postgres"} or (hosted and backend == "sqlite"):
        raise HTTPException(503, "请核对工作台的文档存储配置。")
    return backend == "postgres"


def database_url() -> str | None:
    dsn = os.getenv("SECUREAGENT_DATABASE_URL")
    if not dsn and postgres_documents():
        raise HTTPException(503, "工作台的 PostgreSQL 连接尚未配置。")
    return dsn


@contextmanager
def postgres_connection(dsn: str) -> Iterator[psycopg.Connection]:
    try:
        with psycopg.connect(
            dsn, connect_timeout=5, row_factory=dict_row, prepare_threshold=None
        ) as db:
            yield db
    except psycopg.Error as exc:
        logging.getLogger(__name__).error(
            "workbench database error type=%s sqlstate=%s",
            type(exc).__name__, exc.sqlstate,
        )
        raise HTTPException(503, "工作台数据库暂不可用。") from None


def sign_in(login: str, password: str) -> str | None:
    dsn = database_url()
    if not dsn:
        with closing(accounts.connect()) as db:
            return accounts.sign_in(db, login, password)

    with postgres_connection(dsn) as db:
        account = db.execute(
            """SELECT salt, digest, is_active FROM secureagent.wb_accounts
               WHERE login = %s AND deleted_at IS NULL FOR SHARE""",
            (login,),
        ).fetchone()
        digest = accounts.password_digest(password, account["salt"] if account else bytes(16))
        if not account or not account["is_active"] or not hmac.compare_digest(account["digest"], digest):
            return None

        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        db.execute("DELETE FROM secureagent.wb_sessions WHERE expires_at <= CURRENT_TIMESTAMP")
        db.execute(
            """INSERT INTO secureagent.wb_sessions (token_hash, login, csrf, expires_at)
               VALUES (%s, %s, %s, CURRENT_TIMESTAMP + %s::interval)""",
            (accounts.fingerprint(token), login, csrf, timedelta(hours=accounts.HOURS)),
        )
    return token


def session(token: str | None) -> dict | None:
    if not token or len(token) > 100 or not token.isascii():
        return None

    dsn = database_url()
    if not dsn:
        with closing(accounts.connect()) as db:
            user = accounts.session(db, token)
            if user is None:
                return None
            projects = user["projects"]
            names = {}
            if projects:
                placeholders = ",".join("?" for _ in projects)
                names = dict(db.execute(
                    f"SELECT project_id, display_name FROM project_names WHERE project_id IN ({placeholders})",
                    projects,
                ))
        return {
            **user, "is_company_admin": False,
            "project_roles": dict.fromkeys(projects, user["role"]), "project_names": names,
            "project_states": dict.fromkeys(projects, "active"),
        }

    with postgres_connection(dsn) as db:
        rows = db.execute(
            """SELECT a.login, a.is_company_admin, s.csrf, p.project_id, m.role, p.display_name, p.state
               FROM secureagent.wb_sessions AS s
               JOIN secureagent.wb_accounts AS a ON a.login = s.login
               LEFT JOIN secureagent.project_members AS m ON m.login = a.login
               LEFT JOIN secureagent.projects AS p ON p.project_id = m.project_id AND p.state <> 'deleted'
               WHERE s.token_hash = %s AND s.expires_at > CURRENT_TIMESTAMP AND a.is_active AND a.deleted_at IS NULL
               ORDER BY m.project_id""",
            (accounts.fingerprint(token),),
        ).fetchall()
    if not rows:
        return None
    account = rows[0]
    roles = {row["project_id"]: row["role"] for row in rows if row["project_id"] is not None}
    names = {row["project_id"]: row["display_name"] for row in rows if row["project_id"] is not None}
    states = {row["project_id"]: row["state"] for row in rows if row["project_id"] is not None}
    distinct_roles = set(roles.values())
    return {
        "login": account["login"], "is_company_admin": account["is_company_admin"],
        "csrf": account["csrf"], "projects": list(roles), "project_roles": roles,
        "project_names": names, "project_states": states,
        "role": next(iter(distinct_roles)) if len(distinct_roles) == 1 else None,
    }


@contextmanager
def document_write(project: str, login: str, role: str) -> Iterator[psycopg.Connection | None]:
    """Keep the project writable and the actor authorized until the register commits."""
    dsn = database_url()
    if not dsn:
        # Standalone SQLite demo has no company project lifecycle.
        yield None
        return

    with postgres_connection(dsn) as db:
        # Serialize cloud writes within the project, including approvals with no previous effective version.
        lock = "FOR UPDATE" if postgres_documents() else "FOR SHARE"
        row = db.execute("SELECT state FROM secureagent.projects WHERE project_id = %s " + lock, (project,)).fetchone()
        if row is None or row["state"] == "deleted":
            raise HTTPException(404, "项目或资料不存在。")
        account = db.execute(
            """SELECT 1 FROM secureagent.wb_accounts WHERE login = %s
               AND is_active AND deleted_at IS NULL FOR SHARE""", (login,),
        ).fetchone()
        if account is None:
            raise HTTPException(401, "请先登录。")
        member = db.execute(
            "SELECT role FROM secureagent.project_members WHERE project_id = %s AND login = %s",
            (project, login),
        ).fetchone()
        if member is None:
            raise HTTPException(404, "项目或资料不存在。")
        if member["role"] != role:
            raise HTTPException(403, "此操作需要相应的岗位权限。")
        if row["state"] == "archived":
            raise HTTPException(409, "工程已归档，文档只读。")
        yield db


def sign_out(token: str) -> None:
    fingerprint = accounts.fingerprint(token)
    dsn = database_url()
    if not dsn:
        with closing(accounts.connect()) as db, db:
            db.execute("DELETE FROM wb_sessions WHERE token_hash = ?", (fingerprint,))
        return
    with postgres_connection(dsn) as db:
        db.execute("DELETE FROM secureagent.wb_sessions WHERE token_hash = %s", (fingerprint,))
