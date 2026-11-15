"""Local document-control demo: migrate, batch receive, review, approve and publish."""

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from app.ingest_construction import OUTPUT, ROOT, SOURCE_DIR, extract_sections


DATA = ROOT / "data"
REGISTER = DATA / "construction_control.sqlite3"
ARCHIVE = DATA / "construction_archive"
FILENAME = re.compile(r"(?P<doc>[A-Za-z0-9][A-Za-z0-9_.-]*)_(?P<rev>R[0-9]+)\.md")
SCHEMA = """
CREATE TABLE IF NOT EXISTS project_names (
    project_id TEXT PRIMARY KEY, display_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','effective','superseded')),
    sha256 TEXT NOT NULL,
    chunks TEXT NOT NULL,
    PRIMARY KEY(project_id, document_id, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_per_document
    ON versions(project_id, document_id) WHERE status = 'effective';
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    actor TEXT NOT NULL
);
"""


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def archive(raw, digest, extension=".md"):
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    destination = ARCHIVE / f"{digest}{extension}"
    if destination.exists():
        if destination.read_bytes() != raw:
            raise ValueError(f"归档副本校验失败：{destination.name}")
    else:
        with TemporaryDirectory(dir=ARCHIVE) as temporary:
            stage = Path(temporary) / ("source" + extension)
            stage.write_bytes(raw)
            stage.replace(destination)


def parse_version(raw, extension, source, document_id, title=None):
    if extension == ".md":
        text = raw.decode("utf-8-sig")
        if title is None:
            title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), "")
        if not title:
            raise ValueError("需要以 # 开头的文件标题")
        sections = extract_sections(text, source)
    else:
        from app.office_sources import parse_office
        title, sections = parse_office(raw, extension, source, document_id)
    return title, sections


def record_version(db, key, raw, status, actor, title=None, *, extension=".md"):
    if extension not in {".md", ".docx", ".pdf"}:
        raise ValueError("只接收 Markdown、DOCX 或文本 PDF")
    digest = hashlib.sha256(raw).hexdigest()
    existing = db.execute(
        "SELECT sha256 FROM versions WHERE project_id=? AND document_id=? AND revision=?", key
    ).fetchone()
    if existing:
        if existing["sha256"] != digest:
            raise ValueError(f"{'/'.join(key)} 已登记不同内容，请使用新的修订号")
        return False

    # Validate before archiving so citations refer to this exact snapshot.
    source = (ARCHIVE / f"{digest}{extension}").relative_to(ROOT).as_posix()
    title, sections = parse_version(raw, extension, source, key[1], title)
    archive(raw, digest, extension)
    db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?)",
               (*key, title, status, digest, json.dumps(sections, ensure_ascii=False)))
    db.execute("INSERT INTO events(action,project_id,document_id,revision,actor) VALUES (?,?,?,?,?)",
               ("received:" + status, *key, actor))
    return True


def migrate(db):
    catalogs = [SOURCE_DIR / "catalog.json", *sorted(SOURCE_DIR.glob("catalog_*.json"))]
    count = 0
    with transaction(db):
        for path in catalogs:
            try:
                catalog = json.loads(path.read_text(encoding="utf-8-sig"))
                for version in catalog["revisions"]:
                    source = (SOURCE_DIR / version["source"]).resolve()
                    if source.parent != SOURCE_DIR or source.suffix.lower() != ".md":
                        raise ValueError(f"原文路径无效：{version['source']}")
                    key = (catalog["project_id"], catalog["document_id"], version["revision"])
                    count += record_version(db, key, source.read_bytes(), version["status"], "legacy",
                                            title=catalog["title"])
            except (ValueError, KeyError, TypeError, OSError, sqlite3.IntegrityError) as exc:
                raise ValueError(f"{path.name}：{exc}") from exc
    return count


def receive(db, project, folder, actor):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", project) or not actor.strip():
        raise ValueError("项目编号需为字母/数字/下划线/连字符，经办人不能为空")
    if folder is None:
        folder = DATA / "inbox" / project
    if folder.name != project:
        raise ValueError(f"请选择属于 {project} 的同名收件目录，例如 data/inbox/{project}")
    if not folder.is_dir():
        raise ValueError(f"收件目录不存在：{folder}")
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".md")
    if not files:
        raise ValueError(f"{folder} 内没有待收件的 Markdown 原文")
    count = 0
    with transaction(db):
        for path in files:
            match = FILENAME.fullmatch(path.name)
            if not match:
                raise ValueError(f"{path.name}：文件名应为 文号_R01.md 等格式")
            key = (project, match["doc"], match["rev"])
            try:
                count += record_version(db, key, path.read_bytes(), "draft", actor)
            except (ValueError, OSError, sqlite3.IntegrityError) as exc:
                raise ValueError(f"{path.name}：{exc}") from exc
    return count, len(files) - count


def approve(db, key, reviewer, *, expected_current=...):
    if not reviewer.strip():
        raise ValueError("审批人不能为空")
    with transaction(db):
        row = db.execute(
            "SELECT status FROM versions WHERE project_id=? AND document_id=? AND revision=?", key
        ).fetchone()
        if not row:
            raise ValueError(f"未收件：{'/'.join(key)}")
        if row["status"] == "effective":
            return False
        if row["status"] != "draft":
            raise ValueError(f"{'/'.join(key)} 不是待审批草稿")
        old = db.execute(
            "SELECT revision FROM versions WHERE project_id=? AND document_id=? AND status='effective'",
            key[:2],
        ).fetchone()
        if expected_current is not ... and expected_current != (old["revision"] if old else None):
            raise ValueError("现行版本已变化，请刷新预览后重新审批。")
        if old:
            db.execute("UPDATE versions SET status='superseded' WHERE project_id=? AND document_id=? AND revision=?",
                       (*key[:2], old["revision"]))
            db.execute("INSERT INTO events(action,project_id,document_id,revision,actor) VALUES (?,?,?,?,?)",
                       ("superseded", *key[:2], old["revision"], reviewer))
        db.execute("UPDATE versions SET status='effective' WHERE project_id=? AND document_id=? AND revision=?", key)
        db.execute("INSERT INTO events(action,project_id,document_id,revision,actor) VALUES (?,?,?,?,?)",
                   ("approved", *key, reviewer))
    return True


def publish(db):
    # Serialize concurrent export calls; a reader sees a complete old or new JSONL.
    with transaction(db):
        rows = db.execute("SELECT * FROM versions ORDER BY project_id,document_id,revision").fetchall()
        if not rows:
            raise ValueError("台账没有文档，请先 migrate 或 receive；未替换现有索引")
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with TemporaryDirectory(dir=OUTPUT.parent) as temporary:
            stage = Path(temporary) / "chunks.jsonl"
            with stage.open("w", encoding="utf-8") as target:
                for row in rows:
                    meta = {k: row[k] for k in ("project_id", "document_id", "revision", "status", "title")}
                    for section in json.loads(row["chunks"]):
                        slug = "/".join((row["project_id"], row["document_id"], row["revision"],
                                         section.get("fragment_id", section["section"])))
                        target.write(json.dumps(dict(chunk_id=slug, **meta, **section), ensure_ascii=False) + "\n")
                        count += 1
            stage.replace(OUTPUT)
    return count


def republish_if_registered(register):
    with closing(sqlite3.connect(register, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM versions LIMIT 1").fetchone():
            return None
        return publish(db)


def main():
    parser = argparse.ArgumentParser(description="建筑文控台账：批量收件、审批、发布")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("migrate", help="一次性将初始样本台账迁入 SQLite，成功后发布")
    r = sub.add_parser("receive", help="批量登记一个项目收件目录内的 Markdown，全部为草稿")
    r.add_argument("--project", required=True)
    r.add_argument("--folder", type=Path, help="默认 data/inbox/<项目>；目录名需与项目编号一致")
    r.add_argument("--actor", required=True)
    sub.add_parser("list", help="查看项目、文号、修订号和状态")
    a = sub.add_parser("approve", help="确认一份文档的草稿生效，成功后发布")
    for field in ("project", "document", "revision", "reviewer"):
        a.add_argument("--" + field, required=True)
    sub.add_parser("history", help="查看登记、审批及替代记录（UTC）")
    sub.add_parser("publish", help="从持久台账重建索引；导出失败后也可重复执行")
    args = parser.parse_args()
    REGISTER.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(REGISTER, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            if args.action == "migrate":
                print(f"迁移新登记 {migrate(db)} 个版本")
                print(f"已发布 {publish(db)} 个条款")
            elif args.action == "receive":
                received, skipped = receive(db, args.project, args.folder, args.actor)
                print(f"新增草稿 {received}，相同版本跳过 {skipped}；现行索引未改变")
            elif args.action == "approve":
                changed = approve(db, (args.project, args.document, args.revision), args.reviewer)
                print("审批已登记" if changed else "该版本已生效")
                print(f"已发布 {publish(db)} 个条款")
            elif args.action == "publish":
                print(f"已发布 {publish(db)} 个条款")
            elif args.action == "list":
                for row in db.execute("SELECT project_id,document_id,revision,status,title FROM versions ORDER BY 1,2,3"):
                    print(" | ".join(row))
            else:
                for row in db.execute("SELECT created_utc,action,project_id,document_id,revision,actor FROM events ORDER BY id"):
                    print(" | ".join(row))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"文控操作失败：{exc}\n")


if __name__ == "__main__":
    main()
