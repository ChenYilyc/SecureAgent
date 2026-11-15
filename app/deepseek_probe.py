import os
from getpass import getpass

import httpx2

from app.read_call import ReadDocumentArgs


def main() -> None:
    api_key = (os.getenv("DEEPSEEK_API_KEY") or getpass("DeepSeek API Key: ")).strip()
    if not api_key:
        raise SystemExit("需要提供 DeepSeek API Key。")

    function_spec = {
        "name": "read_document",
        "description": "Read one knowledge-base document by its ID.",
        "parameters": ReadDocumentArgs.model_json_schema(),
    }

    try:
        response = httpx2.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "请使用工具读取文档 A。"}],
                "tools": [{"type": "function", "function": function_spec}],
                "tool_choice": "auto",
                "thinking": {"type": "disabled"},
                "max_tokens": 128,
            },
            timeout=45.0,
        )
    except httpx2.RequestError as exc:
        raise SystemExit(f"连接 DeepSeek 失败：{type(exc).__name__}") from None

    if response.status_code != 200:
        raise SystemExit(f"DeepSeek 返回 HTTP {response.status_code}")

    choice = response.json()["choices"][0]
    calls = choice["message"].get("tool_calls") or []
    print("finish_reason:", choice["finish_reason"])

    if choice["finish_reason"] != "tool_calls" or len(calls) != 1:
        raise SystemExit(f"预期一条完整的工具调用，实际收到 {len(calls)} 条。")

    call = calls[0]
    if call["type"] != "function" or call["function"]["name"] != function_spec["name"]:
        raise SystemExit("模型返回了未登记的工具。")

    args = ReadDocumentArgs.model_validate_json(call["function"]["arguments"])
    print("tool:", call["function"]["name"])
    print("arguments:", args.model_dump_json())


if __name__ == "__main__":
    main()