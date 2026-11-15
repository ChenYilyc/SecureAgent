"""Replay the construction search benchmark locally against a fixed demo corpus."""
import argparse
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import sys

from app.search_construction import search_current
from app.semantic_retrieval import RetrievalUnavailable


FIXTURES = Path(__file__).with_name("retrieval")


@dataclass(frozen=True)
class Case:
    case_id: str
    project: str
    question: str
    expected: str | None


@dataclass(frozen=True)
class Result:
    case: Case
    candidates: tuple[str, ...]
    rank: int | None
    invalid: tuple[str, ...]
    evidence: dict[str, str] = field(default_factory=dict, repr=False, compare=False)


def load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as source:
        return [json.loads(line) for line in source if line.strip()]


def evaluate(mode: str = "lexical") -> tuple[str, list[Result]]:
    index = FIXTURES / "corpus.jsonl"
    corpus = load_rows(index)
    questions = load_rows(FIXTURES / "questions.jsonl")
    logical_input = json.dumps([corpus, questions], ensure_ascii=False, sort_keys=True)
    fingerprint = sha256(logical_input.encode("utf-8")).hexdigest()[:12]
    scope = {row["chunk_id"]: (row["project_id"], row["status"]) for row in corpus}
    texts = {row["chunk_id"]: row["text"] for row in corpus}
    cases = [Case(**row) for row in questions]
    if len(scope) != len(corpus) or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("题号和条款 ID 必须各自唯一。")

    results = []
    for case in cases:
        allowed = (case.project, "effective")
        if case.expected is not None and scope.get(case.expected) != allowed:
            raise ValueError(f"{case.case_id} 的标注需要指向该项目的现行条款。")
        hits = search_current(case.project, case.question, index_path=index, mode=mode)
        ids = tuple(str(row["chunk_id"]) for row in hits[:3])
        invalid = tuple(cid for cid in ids if scope.get(cid) != allowed)
        rank = ids.index(case.expected) + 1 if case.expected in ids else None
        evidence = {cid: texts[cid] for cid in ids if cid not in invalid}
        results.append(Result(case, ids, rank, invalid, evidence))
    return fingerprint, results


def score_ranks(ranks: list[int | None]) -> tuple[int, int, float]:
    if not ranks:
        raise ValueError("排名指标需要至少一道有依据的题。")
    found = [rank for rank in ranks if rank is not None]
    return found.count(1), len(found), sum(1 / rank for rank in found) / len(ranks)


def report(fingerprint: str, results: list[Result]) -> int:
    positive = [r for r in results if r.case.expected is not None]
    negative = [r for r in results if r.case.expected is None]
    first, top_three, reciprocal = score_ranks([r.rank for r in positive])
    invalid_count = sum(len(r.invalid) for r in results)

    print(f"题集与语料指纹 {fingerprint} | {len(positive)} 道有依据 | {len(negative)} 道缺依据")
    print(f"Hit@1  {first}/{len(positive)}  ({first / len(positive):.1%})")
    print(f"Hit@3  {top_three}/{len(positive)}  ({top_three / len(positive):.1%})")
    print(f"MRR@3  {reciprocal:.3f}")
    print(f"越界或未知引用出现次数  {invalid_count}")
    print(f"缺依据题中返回了候选  {sum(bool(r.candidates) for r in negative)}/{len(negative)}")

    for r in results:
        if r.invalid:
            print(f"[引用异常] {r.case.case_id} {' | '.join(r.invalid)}")
        if r.case.expected is not None and r.rank != 1:
            position = f"第 {r.rank} 名" if r.rank else "未进前三"
            print(f"\n{r.case.case_id} [{r.case.project}] {r.case.question}")
            print(f"  标注  {r.case.expected} ({position})")
            print("  候选  " + (" | ".join(r.candidates) or "无"))
        elif r.case.expected is None:
            first_candidate = r.candidates[0] if r.candidates else "无"
            print(f"[缺依据] {r.case.case_id} {r.case.question}  首条候选  {first_candidate}")
    return int(invalid_count > 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="在同一演示语料上测量检索")
    parser.add_argument("--compare", action="store_true", help="比较字面检索和本地 BGE 混合检索")
    args = parser.parse_args()
    fingerprint, baseline = evaluate("lexical")
    print("字面检索")
    status = report(fingerprint, baseline)
    if args.compare:
        try:
            hybrid_fingerprint, hybrid = evaluate("hybrid")
        except RetrievalUnavailable as exc:
            parser.exit(2, f"{exc}\n")
        if fingerprint != hybrid_fingerprint:
            parser.exit(2, "测评期间语料或题集发生了变化，请重跑。\n")
        print("\n混合检索")
        status = max(status, report(hybrid_fingerprint, hybrid))
        print("\n有依据题的条款名次（字面 -> 混合）")
        for old, new in zip(baseline, hybrid, strict=True):
            if old.case.expected:
                print(f"{old.case.case_id}  {old.rank or '未命中'} -> {new.rank or '未命中'}   {old.case.expected}")
    return status


if __name__ == "__main__":
    sys.exit(main())
