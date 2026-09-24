"""OpenAI 兼容 HTTP 层。

架构：API-first。
    请求体用实测模板（templates/*.json）在浏览器上下文里发出去，
    读 SSE 响应体解析成增量。**不点任何界面元素**。

对外做四件事：
  1. 校验可选的 API Key
  2. 代码层校验（字段类型、必填项、空 prompt、n=1）
  3. 请求前检测人机验证；命中则把浏览器拉到前台 + 系统通知 + 明确报错
  4. 把增量流包装成 OpenAI 的 SSE / 完整响应

注意：**不做应用层自检**（不探活、不体检、不跑用例）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import config, sel
from .api_driver import TemplateMissing, get_api_driver
from .browser import get_session
from .driver import flatten_messages, _content_to_text
from .quota import QuotaError, get_fetcher
from .registry import (
    DEFAULT_MODEL,
    MODELS,
    missing_templates,
    openai_model_list,
    resolve,
)

log = logging.getLogger("BrowserViewer.server")

app = FastAPI(title="DouBao2.1API", version="0.2.0")

# 全局串行：只有一个浏览器页面，两次请求必须排队
_SERIAL = asyncio.Lock()
_LAST_AT = 0.0

# 风控提示的去重冷却：连续请求失败时不要反复弹窗打扰用户
_CAPTCHA_NOTIFY_COOLDOWN = config.CAPTCHA_NOTIFY_COOLDOWN
_captcha_notified_at = 0.0

# 命中限流后，先把后续请求挡住的时长（避免继续加压、把临时限流变成长期封禁）
_RATE_LIMIT_COOLDOWN = config.RATE_LIMIT_COOLDOWN
_rate_limited_until = 0.0


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]) -> None:
    if not config.API_KEY:
        return
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_api_key:
        token = x_api_key.strip()
    if token != config.API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def _rid() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


# ── 人机验证 ──────────────────────────────────────────────
async def _guard_captcha(page) -> Optional[str]:
    """请求前检查页面有没有人机验证浮层。命中则拉前台 + 通知。"""
    hit = await sel.detect_captcha(page)
    if not hit:
        return None
    log.warning("检测到人机验证浮层：%s", hit)
    await sel.raise_browser_window(page)
    sel.notify_user(  # 同步函数，不要 await
        "豆包要求人机验证",
        "检测到人机验证浮层，请在浏览器窗口完成验证后重试。",
    )
    return hit


# ── 核心：把 api_driver 的流包成 server 需要的形状 ────────
async def _run_api(model_id: str, messages: List[dict], reasoning: Optional[str]):
    """串行执行一次上游请求，产出 (kind, payload)。"""
    global _LAST_AT, _captcha_notified_at, _rate_limited_until
    prompt = flatten_messages(messages)
    if not prompt.strip():
        yield ("error", "prompt 为空")
        return

    sess = get_session()
    drv = get_api_driver()

    async with _SERIAL:
        # 刚被限流过就不要再打上游了 —— 继续加压会把临时限流变成长期封禁。
        # 这里**不阻塞等待**（用户明确要求不要卡住），而是立刻返回明确错误。
        if time.monotonic() < _rate_limited_until:
            left = int(_rate_limited_until - time.monotonic())
            yield (
                "error",
                f"UPSTREAM_CAPTCHA: 上游限流冷却中，还需约 {left} 秒。"
                "请先在浏览器窗口完成人机验证（滑块/图片），或稍后重试。",
            )
            return

        # 最小间隔，避免触发风控
        gap = time.monotonic() - _LAST_AT
        if gap < config.MIN_INTERVAL:
            await asyncio.sleep(config.MIN_INTERVAL - gap)

        page = await sess.page()

        cap = await _guard_captcha(page)
        if cap:
            yield (
                "error",
                f"UPSTREAM_CAPTCHA: 上游要求人机验证（{cap}）。"
                "已把浏览器窗口提到前台并发送系统通知，请人工完成验证后重试。",
            )
            return

        try:
            async for ev in drv.stream(
                model_id, prompt, reasoning_effort=reasoning, session=sess
            ):
                kind, payload = ev["type"], ev["data"]

                # 上游自己报的风控（710022002 / 710022004）。它同样需要人工处理，
                # 而且**往往没有 DOM 浮层**可检测（实测页面里查不到任何验证组件，
                # 错误只在 SSE 里以 error_code=710022004 "rate limited" 出现）。
                if kind == "error" and "UPSTREAM_CAPTCHA" in str(payload):
                    notified = not (
                        _captcha_notified_at == 0.0
                        or time.monotonic() - _captcha_notified_at
                        > _CAPTCHA_NOTIFY_COOLDOWN
                    )
                    if notified:
                        log.info(
                            "%.0f 秒内已通知过，不重复弹窗（避免连续请求反复打扰）",
                            _CAPTCHA_NOTIFY_COOLDOWN,
                        )
                    else:
                        _captcha_notified_at = time.monotonic()
                        # 1) 拉到前台  2) 系统通知  3) 能用 decision 就把验证界面弹出来
                        # 注意 notify_user 是**同步**函数，不能 await（曾经写成 await，
                        # 抛 TypeError 把请求打成 502，且通知根本没发出去）。
                        log.warning("上游报风控，拉起浏览器并通知用户")
                        await sel.raise_browser_window(page)
                        sel.notify_user(
                            "豆包要求人机验证",
                            "上游返回风控（rate limited）。"
                            "请在浏览器窗口完成滑块/图片验证后重试。",
                        )
                        # 有风控凭据就当场把验证界面摆到用户面前，不用干等它自己弹
                        vd_err = ev.get("verify_data") or ""
                        if vd_err:
                            await sel.show_verify(page, vd_err)
                    # 冷却期内不再打上游，避免把临时限流拖成长期封禁
                    _rate_limited_until = time.monotonic() + _RATE_LIMIT_COOLDOWN
                    log.warning(
                        "进入限流冷却 %.0f 秒，期间请求立即返回错误",
                        _RATE_LIMIT_COOLDOWN,
                    )

                elif kind == "done":
                    # 拿到风控凭据就主动把验证界面摆到用户面前（不用干等它自己弹）
                    vd = (payload or {}).get("verify_data") or ""
                    if vd:
                        await sel.show_verify(page, vd)

                yield (kind, payload)
        except TemplateMissing as exc:
            yield ("error", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("上游请求失败")
            yield ("error", f"{type(exc).__name__}: {exc}")
        finally:
            _LAST_AT = time.monotonic()


# ── 基础端点 ──────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "default_model": DEFAULT_MODEL,
        "models": list(MODELS),
        "missing_templates": missing_templates(),
    }


@app.get("/v1/models")
async def list_models(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    _check_auth(authorization, x_api_key)
    return {"object": "list", "data": openai_model_list()}


@app.get("/v1/upstream/models")
async def upstream_models(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    """读回三个模型的实测标识，用于核对 registry 是否与上游一致。"""
    _check_auth(authorization, x_api_key)
    return {
        "object": "list",
        "data": [
            {
                "id": s.id,
                "model_item_key": s.model_item_key,
                "need_deep_think": s.need_deep_think,
                "ui_label": s.ui_label,
                "template_ready": s.template_ready,
                "note": s.note,
            }
            for s in MODELS.values()
        ],
    }


# ── 额度查看（admin）──────────────────────────────────────
_ADMIN_HTML = Path(__file__).resolve().parent / "admin_page.html"


@app.get("/admin/quota")
async def admin_quota(refresh: int = 0):
    """额度 JSON：当前时段(3小时) / 近 7 天 的用量与重置时间。

    抓一次约 7 秒（要开新标签导航），所以带缓存；`?refresh=1` 强制重抓。
    抓取失败时如果手里有旧数据，一并返回（前端会标注是旧数据）。
    """
    fetcher = get_fetcher()
    sess = get_session()
    try:
        data = await fetcher.get(sess, force=bool(refresh))
        return JSONResponse(content=data)
    except QuotaError as exc:
        body = {"error": str(exc)}
        old = fetcher.cached()
        if old:
            body["cached"] = old
        return JSONResponse(status_code=502, content=body)
    except Exception as exc:  # noqa: BLE001
        log.exception("额度抓取异常")
        body = {"error": f"{type(exc).__name__}: {exc}"}
        old = fetcher.cached()
        if old:
            body["cached"] = old
        return JSONResponse(status_code=502, content=body)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """额度查看页面（服务端直接吐 HTML，无前端构建链）。"""
    try:
        return HTMLResponse(_ADMIN_HTML.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取 admin 页面失败：{exc}")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """避免浏览器请求 favicon 时在控制台留下 404。"""
    from fastapi import Response

    return Response(status_code=204)


# ── 聊天 ──────────────────────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    _check_auth(authorization, x_api_key)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="body must be JSON")

    # ── 代码层校验 ──
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="`messages` must be a non-empty array")
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            raise HTTPException(status_code=400, detail=f"messages[{i}] must be an object")
        if "role" not in m:
            raise HTTPException(status_code=400, detail=f"messages[{i}].role is required")
        if "content" not in m:
            # OpenAI 客户端在工具轮次后可能只发 role+tool_calls
            if "tool_calls" not in m:
                raise HTTPException(
                    status_code=400, detail=f"messages[{i}].content is required"
                )
    if body.get("n") not in (None, 1):
        raise HTTPException(status_code=400, detail="only n=1 is supported")

    prompt_text = flatten_messages(messages)
    if not prompt_text.strip():
        raise HTTPException(status_code=400, detail="prompt is empty after flattening")

    model = body.get("model") or DEFAULT_MODEL
    spec = resolve(model)
    stream = bool(body.get("stream"))
    reasoning = body.get("doubao_reasoning") or body.get("reasoning_effort")
    if isinstance(reasoning, str):
        reasoning = reasoning.strip().lower()
        reasoning = {"low": "1", "medium": "2", "high": "3"}.get(reasoning, reasoning)
    rid = _rid()
    created = int(time.time())

    log.info("[%s] %s stream=%s msgs=%d len=%d",
             rid, spec.id, stream, len(messages), len(prompt_text))
    if not spec.template_ready:
        raise HTTPException(
            status_code=503,
            detail=f"缺少模板 {spec.id}，请先运行 capture.bat",
        )

    # ── 非流式 ──
    if not stream:
        text, err, meta = "", "", {}
        async for kind, payload in _run_api(spec.id, messages, reasoning):
            if kind == "delta":
                text += payload
            elif kind == "error":
                err = payload
            elif kind == "done":
                meta = payload
        if err and not text:
            raise HTTPException(status_code=502, detail=err)
        if not text and not err:
            raise HTTPException(status_code=502, detail="上游没有返回任何正文")
        return JSONResponse(
            content={
                "id": rid,
                "object": "chat.completion",
                "created": created,
                "model": spec.id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": _approx_tokens(messages),
                    "completion_tokens": _approx_tokens_text(text),
                    "total_tokens": _approx_tokens(messages) + _approx_tokens_text(text),
                },
                "doubao_meta": {**meta, "model_item_key": spec.model_item_key},
            }
        )

    # ── 流式 ──
    async def event_stream():
        yield _sse(
            {
                "id": rid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": spec.id,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )
        try:
            async for kind, payload in _run_api(spec.id, messages, reasoning):
                if kind == "delta":
                    yield _sse(
                        {
                            "id": rid,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": spec.id,
                            "choices": [
                                {"index": 0, "delta": {"content": payload}, "finish_reason": None}
                            ],
                        }
                    )
                elif kind == "done":
                    yield _sse(
                        {
                            "id": rid,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": spec.id,
                            "choices": [],
                            "doubao_meta": {**payload, "model_item_key": spec.model_item_key},
                        }
                    )
                elif kind == "error":
                    yield _sse(
                        {
                            "id": rid,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": spec.id,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": f"\n[上游错误] {payload}"},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] 流式异常", rid)
            yield _sse(
                {
                    "id": rid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": spec.id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"\n[上游异常] {type(exc).__name__}: {exc}"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        yield _sse(
            {
                "id": rid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": spec.id,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ── 粗略 token 估算（上游不返回 usage）────────────────────
def _approx_tokens(messages: List[dict]) -> int:
    chars = 0
    for m in messages:
        chars += len(_content_to_text(m.get("content")))
    return max(1, chars // 2) if chars else 0


def _approx_tokens_text(text: str) -> int:
    return max(1, len(text or "") // 2) if text else 0
