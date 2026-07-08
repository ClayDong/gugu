"""Web 问答 API 路由：小白知识库问答 + 概念浏览 + 用户画像。

提供 RESTful 接口供前端 qa.html 调用。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from gugu.utils.log import get_logger

logger = get_logger()

router = APIRouter(prefix="/api", tags=["qa"])

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 问答历史（内存中保留最近20条，重启清空）
_qa_history: list[dict] = []


@router.get("/qa", response_class=HTMLResponse)
async def qa_page() -> str:
    """问答页面。"""
    html_path = STATIC_DIR / "qa.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>qa.html not found</h1>"


@router.post("/qa/ask")
async def ask_question(request: Request) -> JSONResponse:
    """知识库问答接口。

    Body: {"question": "..."}
    Returns: {"answer": "...", "citations": [...], "related_concepts": [...]}
    """
    try:
        body = await request.json()
        question = str(body.get("question", "")).strip()
        if not question:
            return JSONResponse({"error": "问题不能为空"}, status_code=400)

        from gugu.wisdom.kb_agent import get_kb_agent

        agent = get_kb_agent()
        result = await agent.ask(question)

        response_data = result.to_dict()
        # 记录历史
        _qa_history.append(response_data)
        if len(_qa_history) > 20:
            _qa_history.pop(0)

        return JSONResponse(response_data)
    except Exception as e:
        logger.exception(f"[qa-api] 问答失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/qa/history")
async def qa_history() -> JSONResponse:
    """获取问答历史。"""
    return JSONResponse({"history": _qa_history})


@router.get("/qa/stats")
async def qa_stats() -> JSONResponse:
    """获取知识库统计信息。"""
    try:
        from gugu.wisdom.kb_agent import get_kb_agent

        agent = get_kb_agent()
        stats = agent.get_stats()
        return JSONResponse(stats)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/concepts")
async def list_concepts() -> JSONResponse:
    """列出所有概念。"""
    from gugu.wisdom.tutor import _load_concepts, get_progress_stats

    concepts = _load_concepts()
    stats = get_progress_stats()
    return JSONResponse({"concepts": concepts, "stats": stats})


@router.get("/concepts/{concept_id}")
async def get_concept(concept_id: str) -> JSONResponse:
    """获取单个概念详情。"""
    from gugu.wisdom.tutor import get_concept_by_id

    concept = get_concept_by_id(concept_id)
    if not concept:
        return JSONResponse({"error": "概念不存在"}, status_code=404)
    return JSONResponse(concept)


@router.get("/profile")
async def get_profile() -> JSONResponse:
    """获取用户画像。"""
    from gugu.models.user_profile import load_profile

    profile = load_profile()
    return JSONResponse(profile.to_dict())


@router.post("/profile")
async def update_profile(request: Request) -> JSONResponse:
    """更新用户画像。

    Body: {"risk_tolerance": "...", "investment_style": "...", ...}
    """
    try:
        body = await request.json()
        from gugu.models.user_profile import (
            UserProfile,
            rebuild_style_weights,
            save_profile,
        )

        style = body.get("investment_style", "mixed")
        profile = UserProfile(
            risk_tolerance=body.get("risk_tolerance", "balanced"),
            investment_style=style,
            experience=body.get("experience", "novice"),
            horizon=body.get("horizon", "medium"),
            style_weights=rebuild_style_weights(style),
        )
        save_profile(profile)
        return JSONResponse({"status": "ok", "profile": profile.to_dict()})
    except Exception as e:
        logger.exception(f"[qa-api] 更新画像失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
