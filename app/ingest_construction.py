import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "examples" / "construction"
OUTPUT = ROOT / "data" / "construction_chunks.jsonl"


def extract_sections(text: str, source: str) -> list[dict[str, str | int]]:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if not starts:
        raise ValueError(f"{source} 没有找到二级条款标题")
    sections = []
    seen = set()
    for start, end in zip(starts, starts[1:] + [len(lines)]):
        heading = re.fullmatch(r"## +(\d+(?:\.\d+)*)\s+(.+)", lines[start].strip())
        body = "\n".join(lines[start + 1:end]).strip()
        if heading is None or not body:
            raise ValueError(f"{source} 第 {start + 1} 行的条款标题或正文不完整")
        number, title = heading.groups()
        if number in seen:
            raise ValueError(f"{source} 第 {start + 1} 行的条款号重复：{number}")
        seen.add(number)
        sections.append({"section": number, "section_title": title, "source": source,
                         "source_line": start + 1, "text": body})
    return sections


def chunks_from_catalog(
    catalog_path: Path, seen_ids: set[str]
) -> list[dict[str, str | int]]:
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{catalog_path.name} 第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        ) from exc
    project_id = catalog["project_id"]
    document_id = catalog["document_id"]
    chunks = []

    for version in catalog["revisions"]:
        revision = version["revision"]
        source = (SOURCE_DIR / version["source"]).resolve()
        if source.parent != SOURCE_DIR or source.suffix.lower() != ".md":
            raise ValueError(f"原文应为资料目录中的 Markdown 文件：{version['source']}")

        sections = extract_sections(source.read_text(encoding="utf-8-sig"),
                                    source.relative_to(ROOT).as_posix())
        for section in sections:
            chunk_id = f"{project_id}/{document_id}/{revision}/{section['section']}"
            if chunk_id in seen_ids:
                raise ValueError(f"条款标识重复：{chunk_id}")
            seen_ids.add(chunk_id)

            chunks.append({
                "chunk_id": chunk_id,
                "project_id": project_id,
                "document_id": document_id,
                "title": catalog["title"],
                "revision": revision,
                "status": version["status"],
                **section,
            })

    if not chunks:
        raise ValueError("台账中没有可入库的条款")
    return chunks

def build_chunks() -> list[dict[str, str | int]]:
    catalogs = [SOURCE_DIR / "catalog.json", *sorted(SOURCE_DIR.glob("catalog_*.json"))]
    seen_ids: set[str] = set()
    chunks: list[dict[str, str | int]] = []

    for catalog_path in catalogs:
        chunks.extend(chunks_from_catalog(catalog_path, seen_ids))

    current_versions: dict[tuple[str, str], str] = {}

    for row in chunks:
        if row["status"] != "effective":
            continue

        key = (str(row["project_id"]), str(row["document_id"]))
        revision = str(row["revision"])
        first_revision = current_versions.setdefault(key, revision)

        if first_revision != revision:
            raise ValueError(
                f"{key[0]}/{key[1]} 存在多个现行版本："
                f"{first_revision}、{revision}"
            )
    return chunks

def main() -> None:
    # After migration the register owns current status, including manual approvals.
    register = OUTPUT.parent / "construction_control.sqlite3"
    if register.is_file():
        from app.document_control import republish_if_registered
        count = republish_if_registered(register)
        if count is not None:
            print(f"已从文控台账发布 {count} 个条款")
            return
    chunks = build_chunks()
    OUTPUT.parent.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=OUTPUT.parent) as staging:
        candidate = Path(staging) / OUTPUT.name
        records = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in chunks)
        candidate.write_text(records, encoding="utf-8")
        candidate.replace(OUTPUT)

    for row in chunks:
        print(f"{row['chunk_id']} | {row['status']} | {row['source']}:{row['source_line']}")


if __name__ == "__main__":
    main()
