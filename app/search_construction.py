import argparse
import json
import os
from pathlib import Path

from app.ingest_construction import OUTPUT as CHUNKS_PATH
from app.office_sources import source_reference
from app.semantic_retrieval import RetrievalUnavailable, dense_order


POOL_SIZE = 20


def character_pairs(text: str) -> set[str]:
    text = text.casefold()
    pairs = {text[i:i + 2] for i in range(len(text) - 1)}
    return {pair for pair in pairs if pair.isalnum()}


def fuse(lexical: list[int], dense: list[int]) -> list[int]:
    scores: dict[int, float] = {}
    for ranking in (lexical, dense):
        for rank, index in enumerate(ranking, start=1):
            scores[index] = scores.get(index, 0.0) + 1 / (60 + rank)
    dense_rank = {index: rank for rank, index in enumerate(dense)}
    return sorted(scores, key=lambda i: (-scores[i], dense_rank.get(i, len(dense)), i))


def search_current(
    project_id: str, question: str, *, index_path: Path | None = None, mode: str | None = None
) -> list[dict[str, str | int]]:
    if mode is None:
        mode = os.getenv("SECUREAGENT_RETRIEVAL", "lexical")
    if mode not in {"lexical", "hybrid"}:
        raise RetrievalUnavailable("SECUREAGENT_RETRIEVAL 可设为 lexical 或 hybrid。")
    if not any(ch.isalnum() for ch in question):
        return []
    query_pairs = character_pairs(question)
    rows = []
    passages = []
    lexical_hits = []

    source = index_path if index_path is not None else CHUNKS_PATH
    with source.open(encoding="utf-8") as records:
        for line in records:
            if not line.strip():
                continue
            chunk = json.loads(line)

            if chunk["project_id"] != project_id or chunk["status"] != "effective":
                continue

            searchable = " ".join(
                [chunk["title"], chunk["section_title"], chunk["text"]]
            )
            score = len(query_pairs & character_pairs(searchable))
            if mode == "lexical" and not score:
                continue
            index = len(rows)
            rows.append(chunk)
            if score > 0:
                lexical_hits.append((score, index))
            if mode == "hybrid":
                passages.append(searchable)

    lexical_hits.sort(key=lambda hit: hit[0], reverse=True)
    ranking = [index for _, index in lexical_hits[:POOL_SIZE]]
    if mode == "hybrid" and passages:
        dense = dense_order(project_id, question, passages, POOL_SIZE)
        ranking = fuse(ranking, dense)
    return [rows[i] for i in ranking[:3]]


def main() -> None:
    parser = argparse.ArgumentParser(description="查找指定项目的现行条款")
    parser.add_argument("--project", required=True)
    parser.add_argument("--mode", choices=["lexical", "hybrid"])
    parser.add_argument("question")
    args = parser.parse_args()

    try:
        hits = search_current(args.project, args.question, mode=args.mode)
    except RetrievalUnavailable as exc:
        parser.exit(1, f"{exc}\n")
    if not hits:
        print("未检索到当前项目的现行依据。")
        return

    for chunk in hits:
        print(chunk["text"])
        print(f"依据：{chunk['document_id']} · {chunk['revision']} · §{chunk['section']}")
        print(f"原文：{source_reference(chunk)}")


if __name__ == "__main__":
    main()
