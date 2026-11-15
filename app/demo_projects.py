"""Import the fictional construction portfolio as an initial document-control snapshot."""
import argparse
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import tomllib

from app import document_control as control
from app import workbench_accounts as accounts


LIBRARY = Path(__file__).resolve().parents[1] / "examples" / "project_library"
VERSION_FILE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)_(R[0-9]+)\.(md|pdf|docx)")
SNAPSHOT_ACTOR = "demo-library-import"


def register_project(db: sqlite3.Connection, project: str, name: str) -> None:
    row = db.execute("SELECT display_name FROM project_names WHERE project_id=?", (project,)).fetchone()
    if row is not None:
        if row[0] != name:
            raise ValueError(f"项目 {project} 已登记为 {row[0]}，与演示资料的项目名称不同")
        return
    if db.execute("SELECT 1 FROM versions WHERE project_id=? LIMIT 1", (project,)).fetchone():
        raise ValueError(f"项目 {project} 已有其他资料，无法将它登记为演示项目")
    db.execute("INSERT INTO project_names(project_id,display_name) VALUES (?,?)", (project, name))


def import_library(db: sqlite3.Connection, folder: Path = LIBRARY) -> tuple[int, dict[str, str]]:
    with (folder / "catalog.toml").open("rb") as source:
        projects = tomllib.load(source)["projects"]
    names = {}
    seen = set()
    created = 0
    with control.transaction(db):
        has_versions = db.execute("SELECT 1 FROM versions LIMIT 1").fetchone()
        if not has_versions and control.OUTPUT.is_file() and control.OUTPUT.stat().st_size:
            raise ValueError("已有尚未登记到文控台账的条款索引，请先登记现有原件再导入演示项目")
        for project, spec in projects.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", project):
                raise ValueError(f"项目编号无效：{project}")
            name = spec["name"]
            register_project(db, project, name)
            names[project] = name
            for status in ("effective", "draft", "superseded"):
                for filename in spec.get(status, []):
                    match = VERSION_FILE.fullmatch(filename)
                    if not match:
                        raise ValueError(f"样本文号或文件名无效：{filename}")
                    document, revision, ext = match.groups()
                    key = (project, document, revision)
                    if key in seen:
                        raise ValueError(f"清单重复登记 {'/'.join(key)}")
                    seen.add(key)
                    raw = (folder / project / filename).read_bytes()
                    created += control.record_version(db, key, raw, status, SNAPSHOT_ACTOR, extension="." + ext)
    return created, names


def main() -> None:
    parser = argparse.ArgumentParser(description="登记三个虚构建筑项目的样本原件并发布索引")
    parser.parse_args()
    try:
        with closing(accounts.connect()) as db:
            created, names = import_library(db)
            clauses = control.publish(db)
            for project, name in names.items():
                counts = dict(db.execute("SELECT status,count(*) FROM versions WHERE project_id=? GROUP BY status",
                                         (project,)))
                print(f"{project} · {name} | 现行 {counts.get('effective', 0)} | "
                      f"草稿 {counts.get('draft', 0)} | 历史 {counts.get('superseded', 0)}")
            print(f"本次新增 {created} 个版本。整库索引共 {clauses} 个条款片段（含各版本）。")
    except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"演示资料导入失败：{exc}\n")


if __name__ == "__main__":
    main()
