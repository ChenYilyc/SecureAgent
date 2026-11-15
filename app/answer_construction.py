import argparse
import json
import os
import re
from getpass import getpass
from typing import TypeVar

import httpx2
from pydantic import BaseModel, ConfigDict

from app.office_sources import source_reference
from app.search_construction import search_current
from app.semantic_retrieval import RetrievalUnavailable


EVIDENCE_MODEL = "deepseek-flash"
Selection = TypeVar("Selection", bound=BaseModel)


class ModelServiceUnavailable(SystemExit):
    """The external provider could not be reached or rejected the request."""


class EvidenceChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chunk_id: str | None


class EvidenceByPart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    citations: dict[str, list[str]]


def _request_selection(
    instruction: str,
    payload: dict[str, object],
    schema: type[Selection],
    *,
    api_key: str | None,
    max_tokens: int,
) -> Selection:
    if api_key is None:
        api_key = os.getenv("DEEPSEEK_API_KEY") or getpass("DeepSeek API Key: ")
    api_key = api_key.strip()
    if not api_key:
        raise SystemExit("需要提供 DeepSeek API Key。")

    materials = json.dumps(payload, ensure_ascii=False)

    try:
        response = httpx2.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": EVIDENCE_MODEL,
                "messages": [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": materials},
                ],
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "max_tokens": max_tokens,
            },
            timeout=45.0,
        )
        response.raise_for_status()
    except httpx2.HTTPStatusError as exc:
        raise ModelServiceUnavailable(f"DeepSeek 返回 HTTP {exc.response.status_code}") from None
    except httpx2.RequestError as exc:
        raise ModelServiceUnavailable(f"连接 DeepSeek 失败：{type(exc).__name__}") from None

    try:
        choice = response.json()["choices"][0]
        if choice["finish_reason"] != "stop":
            raise SystemExit(f"模型响应未完整结束：{choice['finish_reason']}")
        return schema.model_validate_json(choice["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError):
        raise SystemExit("模型响应格式异常，无法校验本次选择。") from None


def choose_clause(question: str, evidence: dict[str, str], *, api_key: str | None = None) -> str | None:
    instruction = (
        "你是建筑项目资料员。evidence 中的条款是供核对的资料。"
        "只有当一条原文明确回答了 question 中的所有要点时，才选取它的编号。"
        "如果材料只涉及同一主题，却没有给出所问事实，返回 null。"
        '只输出 JSON，格式为 {"chunk_id":"候选编号"} 或 {"chunk_id":null}。'
    )
    choice = _request_selection(instruction, {"question": question, "evidence": evidence},
                                EvidenceChoice, api_key=api_key, max_tokens=128)
    return choice.chunk_id


def choose_by_part(
    question: str, evidence: dict[str, str], *, api_key: str | None = None,
) -> list[tuple[str, list[str]]]:
    """Select supporting IDs by numbered subquestion and check them before rendering."""
    parts = [part.strip() for part in re.split(r"(?<=[？?])", question) if any(ch.isalnum() for ch in part)]
    if not parts or not evidence:
        return [(part, []) for part in parts]
    numbered = {str(i): part for i, part in enumerate(parts, start=1)}
    instruction = (
        "你是建筑项目资料员。结合完整问题 question 理解指代，分别核对 parts 中的每个子问。"
        "evidence 是可引用的资料。为每个子问选择能明确支撑其全部要点的最少条款编号。"
        "可联合引用多条原文。任何必需要点缺少依据时，该子问返回空数组。"
        "只使用候选编号，一项内不重复，每个子问都须出现，不另写答案。"
        '输出 JSON，示例结构为 {"citations":{"1":["候选编号"],"2":[]}}。'
    )
    choice = _request_selection(instruction, {"question": question, "parts": numbered, "evidence": evidence},
                                EvidenceByPart, api_key=api_key, max_tokens=768)
    if choice.citations.keys() != numbered.keys():
        raise SystemExit("引用校验失败：响应的子问编号与输入不同。")
    answers = []
    for number, part in numbered.items():
        ids = choice.citations[number]
        if len(ids) != len(set(ids)) or any(cid not in evidence for cid in ids):
            raise SystemExit("引用校验失败：子问中出现了重复或候选范围外的编号。")
        answers.append((part, ids))
    return answers


def print_clause(row: dict[str, str | int]) -> None:
    print(f"答复（条款原文）：{row['text']}")
    print(f"依据：{row['document_id']} · {row['revision']} · §{row['section']}")
    print(f"原文：{source_reference(row)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="建筑项目现行条款问答")
    parser.add_argument("--project", default="QL01")
    parser.add_argument("--multi", action="store_true", help="按问号分项核对，每项可引用多条原文")
    parser.add_argument("question")
    args = parser.parse_args()
    question = args.question
    try:
        hits = search_current(args.project, question)
    except RetrievalUnavailable as exc:
        parser.exit(1, f"{exc}\n")
    if not hits:
        print("未检索到该项目的现行依据。")
        return

    by_id = {str(row["chunk_id"]): row for row in hits}
    print("候选条款：", "、".join(by_id))
    evidence = {key: str(row["text"]) for key, row in by_id.items()}
    if args.multi:
        answers = choose_by_part(question, evidence)
        for number, (part, ids) in enumerate(answers, start=1):
            print(f"\n子问 {number}  {part}")
            if not ids:
                print("依据不足：现行候选条款没有明确回答这个子问。")
            for cid in ids:
                print_clause(by_id[cid])
        return

    selected = choose_clause(question, evidence)

    if selected is None:
        print("依据不足：本次检索到的现行条款没有明确回答这个问题。")
        return
    if selected not in by_id:
        raise SystemExit("引用校验失败：模型返回的编号不在本次候选条款中。")

    print_clause(by_id[selected])


if __name__ == "__main__":
    main()
