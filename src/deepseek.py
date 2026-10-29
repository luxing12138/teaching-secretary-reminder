"""DeepSeek notice extraction.

The key is supplied at run time by the local desktop application or through the
DEEPSEEK_API_KEY environment variable.  It is never written to the task
database, exported backups, logs, or command output.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - explained to the user at runtime
    requests = None


class DeepSeekError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end < start:
        raise DeepSeekError("模型没有返回可读取的 JSON，请改为手工填写。")
    try:
        value = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DeepSeekError("模型返回的 JSON 格式无效，请改为手工填写。") from exc
    if not isinstance(value, dict):
        raise DeepSeekError("模型返回的数据不是对象。")
    return value


def parse_notice(
    notice_text: str,
    api_key: str,
    model: str = "deepseek-chat",
    base_url: str = "https://api.deepseek.com",
    today: date | None = None,
) -> dict[str, Any]:
    """Return a *draft*, never a task that has been saved automatically."""
    if not notice_text.strip():
        raise DeepSeekError("请先输入通知文本。")
    if not api_key.strip():
        raise DeepSeekError("请在本地设置页输入 DeepSeek API Key，或设置 DEEPSEEK_API_KEY。")
    if requests is None:
        raise DeepSeekError("缺少 requests 依赖。请运行 pip install -r requirements.txt。")
    today = today or date.today()
    system = """你是高校教学秘书事务助手。仅从通知中提取事实，绝不猜测缺失信息。
输出严格 JSON，不要 markdown。字段：
title（简洁事项名，必填），details（保留通知摘要），due_at（YYYY-MM-DDTHH:MM，若仅给日期则写 YYYY-MM-DDT17:00；无法确定则空字符串），
recipients（字符串数组），source_department，category，source_notice，source_id，semester，related_files（字符串数组），confidence（0到1），assumptions（字符串数组）。
当前日期是 %s。相对日期必须转换为绝对日期；未给年份时仅在可合理推断为当前或下一年度时转换，并把推断写入 assumptions。""" % today.isoformat()
    try:
        response = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": notice_text}],
            },
            timeout=45,
        )
    except Exception as exc:
        raise DeepSeekError(f"无法连接 DeepSeek：{exc}") from exc
    if response.status_code >= 400:
        message = response.text[:500]
        raise DeepSeekError(f"DeepSeek 请求失败（HTTP {response.status_code}）：{message}")
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise DeepSeekError("DeepSeek 响应结构异常。") from exc
    return _extract_json(content)
