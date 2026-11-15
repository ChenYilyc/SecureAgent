"""Read the workbench register and its original files from PostgreSQL."""
from datetime import datetime, timezone
import hashlib

from fastapi import HTTPException
import psycopg
from psycopg.types.json import Jsonb

from app import document_control as control
from app import workbench_auth as auth


# Filter by today's membership, account and project state on every read.
VISIBLE = """
    FROM secureagent.document_versions AS v
    JOIN secureagent.projects AS p ON p.project_id=v.project_id AND p.state <> 'deleted'
    JOIN secureagent.project_members AS m ON m.project_id=v.project_id
    JOIN secureagent.wb_accounts AS a ON a.login=m.login AND a.is_active AND a.deleted_at IS NULL
    WHERE m.login=%s AND v.project_id=%s AND (m.role <> 'employee' OR v.status='effective')
"""
Key = tuple[str, str, str]


def read_connection():
    dsn = auth.database_url()
    if not dsn:
        raise HTTPException(503, "工作台的 PostgreSQL 连接尚未配置。")
    return auth.postgres_connection(dsn)


def list_versions(project: str, login: str) -> list[dict]:
    with read_connection() as db:
        return db.execute(
            "SELECT v.document_id,v.revision,v.status,v.title " + VISIBLE +
            """ ORDER BY CASE v.status WHEN 'draft' THEN 0 WHEN 'effective' THEN 1 ELSE 2 END,
                         v.document_id, v.revision""", (login, project),
        ).fetchall()


def preview(key: Key, login: str) -> dict:
    with read_connection() as db:
        row = db.execute(
            """SELECT v.title, v.status, v.sha256, v.extension, v.chunks,
                      CASE WHEN m.role='employee' THEN '[]'::jsonb ELSE v.events END AS events,
                      (SELECT cur.revision FROM secureagent.document_versions AS cur
                       WHERE cur.project_id=v.project_id AND cur.document_id=v.document_id
                         AND cur.status='effective') AS current """ + VISIBLE +
            " AND v.document_id=%s AND v.revision=%s", (login, *key),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "资料不可用。")
    return row


def original(key: Key, login: str) -> dict:
    with read_connection() as db:
        row = db.execute(
            "SELECT v.extension, v.original_bytes, v.sha256 " + VISIBLE +
            " AND v.document_id=%s AND v.revision=%s", (login, *key),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "资料不可用。")
    return row


def current_chunks(project: str, login: str) -> list[dict]:
    with read_connection() as db:
        versions = db.execute(
            "SELECT v.document_id, v.revision, v.title, v.chunks " + VISIBLE +
            " AND v.status='effective' ORDER BY v.document_id,v.revision", (login, project),
        ).fetchall()
    rows = []
    for version in versions:
        doc, rev = version["document_id"], version["revision"]
        for part in version["chunks"]:
            section = part.get("fragment_id", part["section"])
            rows.append({**part, "project_id": project, "document_id": doc, "revision": rev,
                         "status": "effective", "title": version["title"],
                         "chunk_id": "/".join((project, doc, rev, section))})
    return rows


def current_hits(project: str, login: str, hits: list[dict]) -> list[dict]:
    if not hits:
        return []
    with read_connection() as db:
        active = db.execute(
            "SELECT v.document_id,v.revision " + VISIBLE + " AND v.status='effective'",
            (login, project),
        ).fetchall()
    keys = {(row["document_id"], row["revision"]) for row in active}
    return [r for r in hits if r["project_id"] == project and (r["document_id"], r["revision"]) in keys]


def event(actor: str, action: str) -> Jsonb:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return Jsonb([{"created_utc": when, "actor": actor, "action": action}])


def receive(db: psycopg.Connection, key: Key, raw: bytes, extension: str, actor: str) -> bool:
    # document_write holds the project's row lock through the insert and commit.
    digest = hashlib.sha256(raw).hexdigest()
    existing = db.execute(
        """SELECT sha256,extension FROM secureagent.document_versions
           WHERE project_id=%s AND document_id=%s AND revision=%s""", key,
    ).fetchone()
    if existing:
        if (existing["sha256"], existing["extension"]) != (digest, extension):
            raise ValueError("此修订版已经登记了不同原件，请使用新的修订号。")
        return False
    source = f"{key[1]}_{key[2]}{extension}"
    title, chunks = control.parse_version(raw, extension, source, key[1])
    db.execute(
        """INSERT INTO secureagent.document_versions
               (project_id,document_id,revision,title,extension,original_bytes,chunks,events)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (*key, title, extension, raw, Jsonb(chunks), event(actor, "received:draft")),
    )
    return True


def approve(db: psycopg.Connection, key: Key, actor: str, sha256: str, expected_current: str | None) -> int:
    target = db.execute(
        """SELECT sha256,status FROM secureagent.document_versions
           WHERE project_id=%s AND document_id=%s AND revision=%s""", key,
    ).fetchone()
    if target is None:
        raise HTTPException(404, "资料不可用。")
    if target["sha256"] != sha256:
        raise HTTPException(409, "与预览的原件不同，请刷新。")
    if target["status"] != "effective":
        if target["status"] != "draft":
            raise HTTPException(409, "该版本已归入历史，请刷新。")
        current = db.execute(
            """SELECT revision FROM secureagent.document_versions
               WHERE project_id=%s AND document_id=%s AND status='effective'""", key[:2],
        ).fetchone()
        previous = current["revision"] if current else None
        if previous != expected_current:
            raise HTTPException(409, "现行版本已变化，请刷新预览后重新审批。")
        if current:
            db.execute(
                """UPDATE secureagent.document_versions SET status='superseded', events=events || %s
                   WHERE project_id=%s AND document_id=%s AND revision=%s""",
                (event(actor, "superseded"), *key[:2], previous),
            )
        db.execute(
            """UPDATE secureagent.document_versions SET status='effective', events=events || %s
               WHERE project_id=%s AND document_id=%s AND revision=%s""",
            (event(actor, "approved"), *key),
        )
    return db.execute(
        """SELECT coalesce(sum(jsonb_array_length(chunks)), 0) AS total
           FROM secureagent.document_versions WHERE project_id=%s AND status='effective'""", (key[0],),
    ).fetchone()["total"]
