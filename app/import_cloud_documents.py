"""Import selected workbench projects from the local register into PostgreSQL."""
import argparse
from collections.abc import Sequence
from contextlib import closing
from getpass import getpass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import document_control as control


def read_register(projects: Sequence[str]) -> tuple[dict[str, str], dict[str, list[dict]]]:
    names: dict[str, str] = {}
    versions: dict[str, list[dict]] = {}
    # Read one consistent snapshot of version status, clauses and audit events.
    uri = control.REGISTER.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as local:
        local.row_factory = sqlite3.Row
        local.execute("BEGIN")
        for code in projects:
            name = local.execute("SELECT display_name FROM project_names WHERE project_id=?", (code,)).fetchone()
            names[code] = name["display_name"] if name else code
            versions[code] = []
            rows = local.execute(
                """SELECT project_id, document_id, revision, title, status, sha256, chunks
                   FROM versions WHERE project_id=? ORDER BY document_id, revision""", (code,)
            )
            for row in rows:
                item = dict(row)
                item["chunks"] = json.loads(row["chunks"])
                item["events"] = [dict(event) for event in local.execute(
                    """SELECT created_utc, action, actor FROM events
                       WHERE project_id=? AND document_id=? AND revision=? ORDER BY id""",
                    (code, row["document_id"], row["revision"]),
                )]
                versions[code].append(item)
    return names, versions


def ensure_project(db: psycopg.Connection, code: str, name: str, run_id: str) -> str:
    inserted = db.execute(
        """INSERT INTO secureagent.projects (project_id, display_name) VALUES (%s, %s)
           ON CONFLICT (project_id) DO NOTHING RETURNING project_id""", (code, name),
    ).fetchone()
    if inserted:
        db.execute(
            """INSERT INTO secureagent.admin_events (request_id, actor_login, action, project_id)
               VALUES (%s, 'db:' || current_user::text, 'project_created', %s)""", (run_id, code),
        )
    return db.execute(
        "SELECT state FROM secureagent.projects WHERE project_id=%s FOR UPDATE", (code,),
    ).fetchone()["state"]


def import_version(db: psycopg.Connection, item: dict) -> bool:
    key = (item["project_id"], item["document_id"], item["revision"])
    label = "/".join(key)
    chunks = item["chunks"]
    if not isinstance(chunks, list) or not chunks or not isinstance(chunks[0], dict):
        raise ValueError(f"{label} 的本地台账缺少条款或来源。")
    source = chunks[0].get("source")
    suffix = Path(source).suffix.lower() if isinstance(source, str) else ""
    digest = item["sha256"]
    if suffix not in {".pdf", ".docx", ".md"} or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{label} 的原件类型或摘要无效。")

    existing = db.execute(
        """SELECT sha256, extension FROM secureagent.document_versions
           WHERE project_id=%s AND document_id=%s AND revision=%s""", key,
    ).fetchone()
    if existing:
        if (existing["sha256"], existing["extension"]) != (digest, suffix):
            raise ValueError(f"{label} 在云端已有不同原件，请核对文号和修订版。")
        return False

    # Read the content-addressed archive, never a path supplied by a clause.
    original = (control.ARCHIVE / (digest + suffix)).read_bytes()
    if hashlib.sha256(original).hexdigest() != digest:
        raise ValueError(f"{label} 的原件与本地台账中的摘要不一致。")
    try:
        db.execute(
            """INSERT INTO secureagent.document_versions
                   (project_id, document_id, revision, title, status, extension, original_bytes, chunks, events)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (*key, item["title"], item["status"], suffix, original, Jsonb(chunks), Jsonb(item["events"])),
        )
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name == "document_versions_one_effective":
            raise ValueError(f"{label} 与云端的现行版冲突，请核对版本状态。") from None
        raise
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="将指定工程的本地文控版本及原件一次性导入云库")
    parser.add_argument("--projects", nargs="+", required=True, metavar="PROJECT")
    projects = sorted(set(parser.parse_args().projects))
    try:
        names, versions = read_register(projects)
        if not any(versions.values()):
            raise ValueError("本地台账未找到所选工程的文档版本，请核对工程编号。")
        dsn = getpass("云端 Supabase Session pooler 数据库连接串（隐藏输入）：").strip()
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("请输入云项目的 PostgreSQL 连接串。")
        run_id = uuid4().hex
        messages = []
        with psycopg.connect(dsn, row_factory=dict_row, sslmode="require",
                             connect_timeout=15, prepare_threshold=None) as db:
            for code in projects:
                if not versions[code]:
                    messages.append(f"{code} | 本地台账无文档，跳过")
                    continue
                state = ensure_project(db, code, names[code], run_id)
                if state != "active":
                    messages.append(f"{code} | 云端工程状态 {state}，跳过")
                    continue
                added = 0
                for item in versions[code]:
                    if import_version(db, item):
                        added += 1
                messages.append(f"{code} | 新增 {added} 个版本，已存在 {len(versions[code]) - added} 个")
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"导入失败：{exc}\n")
    except psycopg.Error as exc:
        # libpq connection messages can contain input, so print only the server's primary diagnostic.
        message = exc.diag.message_primary or "请核对云端连接串、数据库密码和网络。"
        parser.exit(1, f"导入失败：{type(exc).__name__} ({exc.sqlstate or '无 SQLSTATE'})，{message}\n")
    for message in messages:
        print(message)
    print("云端文档导入已提交。")


if __name__ == "__main__":
    main()
