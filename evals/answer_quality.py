"""Score the production evidence selector on the frozen construction QA cases."""
import argparse
from getpass import getpass
import os
import sys

from app.answer_construction import EVIDENCE_MODEL, choose_clause
from app.semantic_retrieval import RetrievalUnavailable
from evals.retrieval_baseline import Result, evaluate


def grade(result: Result, selected: str | None) -> tuple[bool, str]:
    if selected is not None and selected not in result.evidence:
        return False, "候选外引用"
    expected = result.case.expected
    if expected is None:
        if selected is not None:
            return False, "缺依据仍选了条款"
        return True, "有候选，正确拒答" if result.evidence else "检索为空，系统拒答"
    if selected == expected:
        return True, "正确引用"
    if expected not in result.evidence:
        return False, "正确条款未被召回"
    return False, "误拒答" if selected is None else "选错条款"


def run(results: list[Result], api_key: str | None = None) -> int:
    for result in results:
        if result.invalid:
            print(f"{result.case.case_id} 检索范围异常，本轮中止。", file=sys.stderr)
            return 2

    if api_key is None and any(r.evidence for r in results):
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            try:
                api_key = getpass("DeepSeek API Key: ").strip()
            except EOFError:
                api_key = ""
        if not api_key:
            print("需要提供 DeepSeek API Key。", file=sys.stderr)
            return 2

    correct_citations = correct_abstentions = abstentions_with_evidence = 0
    positives = sum(r.case.expected is not None for r in results)
    negatives = len(results) - positives
    for completed, result in enumerate(results):
        case = result.case
        try:
            selected = choose_clause(case.question, result.evidence, api_key=api_key) if result.evidence else None
        except SystemExit as exc:
            print(f"{case.case_id} 模型调用或响应校验失败：{exc}。完成 {completed}/{len(results)} 题，本轮未完成。",
                  file=sys.stderr)
            return 2
        passed, reason = grade(result, selected)
        if passed and case.expected is not None:
            correct_citations += 1
        elif passed:
            correct_abstentions += 1
            abstentions_with_evidence += bool(result.evidence)

        print(f"{case.case_id} [{case.project}] {reason}  标注={case.expected!r}  所选={selected!r}", flush=True)
        if not passed:
            print(f"  问题  {case.question}\n  候选  {' | '.join(result.candidates) or '无'}", flush=True)

    if positives:
        print(f"有依据题正确引用  {correct_citations}/{positives}")
    if negatives:
        print(f"缺依据题正确拒答  {correct_abstentions}/{negatives}")
        exposed = sum(r.case.expected is None and bool(r.evidence) for r in results)
        if exposed:
            print(f"其中有候选时正确拒答  {abstentions_with_evidence}/{exposed}")
    return int(correct_citations + correct_abstentions != len(results))


def main() -> int:
    parser = argparse.ArgumentParser(description="用混合检索和真实 DeepSeek 评估演示题的引用与拒答")
    parser.add_argument("--case", nargs="+", metavar="题号", help="只测指定题，例如 --case P07 N02")
    args = parser.parse_args()
    try:
        fingerprint, results = evaluate("hybrid")
    except (OSError, ValueError, RetrievalUnavailable) as exc:
        parser.exit(2, f"检索准备失败：{exc}\n")
    if args.case:
        requested = set(args.case)
        unknown = sorted(requested - {r.case.case_id for r in results})
        if unknown:
            parser.error("未知题号 " + ", ".join(unknown))
        results = [r for r in results if r.case.case_id in requested]
    calls = sum(bool(r.evidence) for r in results)
    print(f"题集与语料指纹 {fingerprint} | 混合检索 | 模型 {EVIDENCE_MODEL} | 最多 {calls} 次调用", flush=True)
    return run(results)


if __name__ == "__main__":
    sys.exit(main())
