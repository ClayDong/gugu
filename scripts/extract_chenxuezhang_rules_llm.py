#!/usr/bin/env python3
"""陈学长断板反包语义标签抽取（研究层 PoC）。

该脚本只做两件事：
1. 将已有转写/研究材料交给 LLM 做固定字段的语义标签抽取；
2. 校验并保存可审计 JSON。

LLM 不得生成可执行条件，不得输出买卖、仓位或订单结论。本脚本不会驱动策略、
信号融合或自动下单。原先的 ``eval`` 回测路径已移除。

用法：
    LLM_API_KEY=... uv run python scripts/extract_chenxuezhang_rules_llm.py \
        --data-cutoff 2026-08-17T15:00:00+08:00
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.research.rebound_semantics import (  # noqa: E402
    ReboundSemanticLabel,
    SemanticLabelError,
    parse_semantic_label,
)

_RESEARCH_TMP_DIR = Path(tempfile.gettempdir()) / "gugu-research"
TRANSCRIPT_PATH = _RESEARCH_TMP_DIR / "chenxuezhang_transcript.txt"
OUTPUT_PATH = _RESEARCH_TMP_DIR / "chenxuezhang_semantic_label.json"
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
PROMPT_VERSION = "rebound-semantic-v1"


SEMANTIC_SYSTEM_PROMPT = """你是 A 股研究标注员，只做研究层语义抽取，不做交易决策。
必须只输出一个 JSON 对象，字段必须严格为：
{
  "asof": "ISO-8601 时间",
  "source_ids": ["证据 ID"],
  "theme_state": "主线|支线|孤立|未知",
  "theme_strength": "强|中|弱|未知",
  "board_role": "核心|中军|跟风|未知",
  "intraday_acceptance": "强|中|弱|未知",
  "wash_quality": "有效|存疑|无效|未知",
  "uncertainty": 0.0,
  "evidence_spans": ["原始证据片段"],
  "model_version": "模型版本",
  "prompt_version": "rebound-semantic-v1"
}

规则：
- 只使用用户材料和 data_cutoff 之前的证据；无法确认就填“未知”。
- 没有证据片段时，所有语义字段都必须填“未知”。
- 不得输出 buy、sell、signal、position、order、entry、exit、take_profit、stop_loss 等字段。
- 不得输出解释、Markdown、代码块或任何额外字段。
"""


def read_transcript(path: Path) -> str:
    """读取研究材料；缺失文件直接失败，不伪造证据。"""
    return path.read_text(encoding="utf-8")


def _llm_api_key() -> str:
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未配置 LLM_API_KEY，语义抽取安全失败")
    return key


def _call_llm(prompt: str) -> str:
    """调用 LLM，只返回原始文本；结构校验由研究层统一完成。"""
    api_key = _llm_api_key()
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                f"{LLM_BASE_URL.rstrip('/')}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": SEMANTIC_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 1200,
                },
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"LLM API 返回错误: {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"LLM API 请求失败: {exc}") from exc

    try:
        message = data["choices"][0]["message"]
        content = message.get("content", "") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM 返回格式异常") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM 返回内容为空")
    return content


def extract_semantic_label(
    text: str,
    *,
    data_cutoff: str,
    source_ids: list[str],
) -> ReboundSemanticLabel:
    """抽取并校验一个研究层语义标签。"""
    prompt = (
        f"data_cutoff: {data_cutoff}\n"
        f"source_ids: {json.dumps(source_ids, ensure_ascii=False)}\n"
        "研究材料（仅允许使用截止时间前的信息）：\n"
        f"{text[:12000]}"
    )
    content = _call_llm(prompt)
    label = parse_semantic_label(content, data_cutoff=data_cutoff)
    if label["source_ids"] != source_ids:
        raise SemanticLabelError("LLM 返回的 source_ids 与调用方证据 ID 不一致")
    if label["prompt_version"] != PROMPT_VERSION:
        raise SemanticLabelError("LLM 返回的 prompt_version 与调用方协议不一致")
    return label


def main() -> None:
    parser = argparse.ArgumentParser(description="抽取断板反包研究层语义标签")
    parser.add_argument("--transcript", type=Path, default=TRANSCRIPT_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--data-cutoff", required=True, help="输入数据截止时间（ISO-8601）")
    parser.add_argument(
        "--source-id",
        action="append",
        dest="source_ids",
        required=True,
        help="原始证据 ID，可重复传入",
    )
    args = parser.parse_args()

    text = read_transcript(args.transcript)
    label = extract_semantic_label(
        text,
        data_cutoff=args.data_cutoff,
        source_ids=args.source_ids,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(label, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"研究层语义标签已保存: {args.output}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, SemanticLabelError, OSError) as exc:
        print(f"语义抽取失败: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
