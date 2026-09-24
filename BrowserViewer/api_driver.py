"""API-first 上游驱动：在浏览器上下文里自己发 fetch，读 SSE。

为什么要这样（三个子代理精读上游后的一致结论）：
    上游 SeiShonagon520/doubao2api 的浏览器层**几乎没有任何页面交互** ——
    全项目 grep `data-testid|contenteditable|textarea|fill(|press(|keyboard` 零命中。
    它是在 page.evaluate 里自己构造 JSON、自己 fetch、自己读 SSE，
    只有 a_bogus/X-Bogus 签名借页面算（window.bdms.frontierSign）。

    点界面的路线（本项目第一版）在这里会全线崩溃：一旦弹人机验证，
    浮层会盖住整个页面（`<html> intercepts pointer events`），
    输入框、发送按钮、模型选择器全都点不动。

本模块的做法：
    1. 用 tools/capture_templates 录下的真实请求体当模板（字段最稳）
    2. 每次请求只替换文本 / id / 时间戳等易变字段
    3. 把 body 当字符串塞进页面，交给「页面自己的 fetch」发出去
       —— 这样自动继承页面的签名逻辑（X-Bogus/a_bogus 由页面处理），
          我们也完全不碰任何密钥
    4. 在页面里读 ReadableStream，按块通过 expose_function 回传 Python
    5. Python 侧用 SSEAccumulator 解析成 (thinking, text) 增量
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

from . import config
from .browser import BrowserSession, get_session

log = logging.getLogger("BrowserViewer.api_driver")

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"

# 页面里暴露给 JS 的收集函数名
_BRIDGE = "__dshCollect"
_REQUEST_TOKEN = "__dshReqToken"


class TemplateMissing(RuntimeError):
    """没有模板。提示用户跑 capture.bat。"""


def template_path(model_id: str) -> Path:
    return TEMPLATE_DIR / f"{model_id}.json"


def load_template(model_id: str) -> dict:
    p = template_path(model_id)
    if not p.exists():
        raise TemplateMissing(
            f"缺少请求模板 {p.name}。请先运行 capture.bat 抓取三个模型的模板。"
        )
    return json.loads(p.read_text(encoding="utf-8"))


def build_body(template: dict, text: str, *, reasoning_effort: Optional[str] = None) -> str:
    """在模板基础上生成本次请求体。

    只改易变字段，模型标识（model_item_key / need_deep_think / mode_id）原样保留
    —— 这正是"选模型"的实现方式，不再点界面。
    """
    body = json.loads(json.dumps(template["body_json"]))  # 深拷贝

    now_ms = int(time.time() * 1000)
    now_s = int(time.time())
    local_msg_id = str(uuid.uuid4())
    local_conv_id = f"local_{uuid.uuid4().hex[:16]}"
    unique_key = str(uuid.uuid4())

    # client_meta
    cm = body.setdefault("client_meta", {})
    cm["local_conversation_id"] = local_conv_id
    cm["conversation_id"] = ""
    cm["last_section_id"] = ""
    cm["last_message_index"] = None

    # 消息体：只放一个文本块
    msgs = body.get("messages") or [{}]
    msg = msgs[0]
    msg["local_message_id"] = local_msg_id
    msg["message_status"] = 0
    for blk in msg.get("content_block") or []:
        if blk.get("block_type") == 10000:
            blk["block_id"] = str(uuid.uuid4())
            blk["content"] = {
                "text_block": {
                    "text": text,
                    "icon_url": "",
                    "icon_url_dark": "",
                    "summary": "",
                },
                "pc_event_block": "",
            }
            break
    body["messages"] = [msg]

    # option
    opt = body.setdefault("option", {})
    opt["create_time_ms"] = now_ms
    opt["unique_key"] = unique_key
    opt["need_create_conversation"] = True
    if reasoning_effort:
        cie = opt.get("conversation_init_ext")
        if isinstance(cie, dict) and cie:
            cie["reasoning_effort"] = str(reasoning_effort)
        mc = opt.get("model_config")
        if isinstance(mc, dict) and mc:
            try:
                mc["reasoning_effort"] = int(reasoning_effort)
            except (TypeError, ValueError):
                pass
        agg = opt.get("aggregate_params")
        if isinstance(agg, dict) and agg:
            agg["reasoning_effort"] = str(reasoning_effort)

    # 恢复选项里的时间戳
    rec = opt.get("recovery_option")
    if isinstance(rec, dict):
        rec["req_create_time_sec"] = now_s

    # general_task_param 里的 thread_local_message_id 跟着换
    def _fix_gtp(node) -> None:
        if isinstance(node, dict):
            tlm = node.get("thread_local_message_id")
            if isinstance(tlm, list) and tlm:
                node["thread_local_message_id"] = [local_msg_id]
            for v in node.values():
                _fix_gtp(v)
        elif isinstance(node, list):
            for v in node:
                _fix_gtp(v)

    _fix_gtp(opt.get("general_task_param"))
    ext = body.get("ext")
    if isinstance(ext, dict):
        _fix_gtp(ext.get("general_task_param"))

    return json.dumps(body, ensure_ascii=False)


def build_url(template_url: str) -> str:
    """保留模板 URL（含 query），只换掉每次必须变的 web_tab_id。

    模板 URL 里没有 a_bogus —— 因为这是页面内 fetch，签名由页面自己的
    fetch 包装层负责，我们不要手动加。
    """
    base, _, query = template_url.partition("?")
    if not query:
        return template_url
    parts = []
    for kv in query.split("&"):
        if kv.startswith("web_tab_id="):
            parts.append(f"web_tab_id={uuid.uuid4()}")
        elif kv:
            parts.append(kv)
    return f"{base}?{'&'.join(parts)}"


class ApiDriver:
    """模板重放 + 页面内流式读取。"""

    def __init__(self) -> None:
        self._bridge_ready = False
        # request_token -> asyncio.Queue
        self._queues: Dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def _ensure_bridge(self, page) -> None:
        if self._bridge_ready:
            return

        def _on_chunk(payload) -> None:
            """JS 回调：payload = {token, chunk} 或 {token, done:True} 或 {token, error}。"""
            try:
                token = payload.get("token", "")
                q = self._queues.get(token)
                if q is None:
                    return
                q.put_nowait(payload)
            except Exception:  # noqa: BLE001
                log.debug("桥接回调异常", exc_info=True)

        try:
            await page.expose_function(_BRIDGE, _on_chunk)
        except Exception as exc:  # noqa: BLE001
            # 重复注册是正常的，忽略
            if "already been registered" not in str(exc):
                raise
        self._bridge_ready = True
        log.info("JS 桥接已就绪（%s）", _BRIDGE)

    async def stream(
        self,
        model_id: str,
        text: str,
        *,
        reasoning_effort: Optional[str] = None,
        session: Optional[BrowserSession] = None,
    ) -> AsyncIterator[dict]:
        """在页面里发一次请求，产出 {"type": "delta"/"done"/"error", ...}。"""
        from .driver import SSEAccumulator  # 避免循环导入

        tpl = load_template(model_id)
        body_str = build_body(tpl, text, reasoning_effort=reasoning_effort)
        url = build_url(tpl["url"])

        sess = session or get_session()
        page = await sess.page()
        await self._ensure_bridge(page)

        token = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[token] = queue

        acc = SSEAccumulator()
        emitted = ""
        t0 = time.monotonic()

        # 页面内：发起 fetch 并逐块回传。
        #
        # 这段 JS 必须**自己带超时**，不能让 Python 侧负责放弃等待：
        # reader.read() 在上游静默掐断流（风控常见）时会永久挂起，
        # 而 page.evaluate 一旦挂住，整个页面后续所有调用全被堵死
        # —— 实测踩过，症状是服务端假死、浏览器卡住。
        kickoff = """
        async ([url, bodyStr, token, bridgeName, hardLimitMs, idleLimitMs]) => {
            const send = (obj) => { try { window[bridgeName](obj); } catch (e) {} };
            const started = Date.now();
            let lastData = Date.now();
            let reader = null;
            const ctl = new AbortController();
            const killer = setTimeout(() => { try { ctl.abort(); } catch (e) {} }, hardLimitMs);
            try {
                const res = await window.fetch(url, {
                    method: 'POST',
                    headers: { 'content-type': 'application/json', 'accept': 'text/event-stream' },
                    body: bodyStr,
                    credentials: 'include',
                    signal: ctl.signal,
                });
                send({ token, head: { status: res.status, ct: res.headers.get('content-type') || '' } });
                if (!res.body) { send({ token, error: 'no response body' }); return; }
                reader = res.body.getReader();
                const dec = new TextDecoder('utf-8');
                while (true) {
                    if (Date.now() - started > hardLimitMs) {
                        send({ token, error: 'page-side hard timeout' });
                        break;
                    }
                    if (Date.now() - lastData > idleLimitMs) {
                        send({ token, error: 'page-side idle timeout (stream stalled)' });
                        break;
                    }
                    // 给 read() 也套一层超时，否则 read 本身会永久挂住
                    const got = await Promise.race([
                        reader.read(),
                        new Promise(r => setTimeout(() => r({ __tick: true }), 1000)),
                    ]);
                    if (got && got.__tick) continue;
                    const { done, value } = got;
                    if (done) break;
                    lastData = Date.now();
                    send({ token, chunk: dec.decode(value, { stream: true }) });
                }
                send({ token, done: true });
            } catch (e) {
                send({ token, error: String(e) });
            } finally {
                clearTimeout(killer);
                try { if (reader) await reader.cancel(); } catch (e) {}
                try { ctl.abort(); } catch (e) {}
            }
        }
        """

        hard_ms = int(config.REQUEST_TIMEOUT * 1000)
        # 空闲超时：这么久没有任何新数据就认为流被掐断。
        # 不能设太小 —— 长推理时上游会长时间不吐字。
        idle_ms = int(max(config.FIRST_TOKEN_TIMEOUT, 90) * 1000)

        # 不 await：让它和下面的队列消费并行
        eval_task = asyncio.create_task(
            page.evaluate(
                kickoff, [url, body_str, token, _BRIDGE, hard_ms, idle_ms]
            )
        )

        deadline = t0 + config.REQUEST_TIMEOUT
        try:
            while True:
                timeout = max(1.0, deadline - time.monotonic())
                try:
                    item = await asyncio.wait_for(queue.get(), timeout)
                except asyncio.TimeoutError:
                    if not emitted:
                        yield {"type": "error", "data": "等待上游首字超时"}
                    break

                if item.get("head"):
                    head = item["head"]
                    log.info(
                        "[api] status=%s ct=%s", head.get("status"), head.get("ct")
                    )
                    if "event-stream" not in (head.get("ct") or ""):
                        log.warning("[api] 返回不是 SSE，可能是鉴权/风控错误体")
                    continue

                if item.get("chunk"):
                    chunk = item["chunk"]
                    got = 0
                    for _think, piece in acc.feed(chunk):
                        if piece:
                            got += 1
                            emitted += piece
                            yield {"type": "delta", "data": piece}
                    log.debug(
                        "[api] chunk %d 字节 -> %d 段文本 (累计 %d 字)",
                        len(chunk), got, len(emitted),
                    )
                    continue

                if item.get("error"):
                    yield {"type": "error", "data": f"页面内请求失败：{item['error']}"}
                    break

                if item.get("done"):
                    break

            for _think, piece in acc.flush():
                if piece:
                    emitted += piece
                    yield {"type": "delta", "data": piece}

            if acc.risk_error_code:
                # verify_data 一并带出：server 用它主动拉起页面验证界面。
                # 走 error 而不是只走 done，是为了"命中那一刻就弹"，
                # 不必等收尾逻辑（期间可能已被限流冷却拦住）。
                yield {
                    "type": "error",
                    "data": (
                        f"UPSTREAM_CAPTCHA: 上游风控（code={acc.risk_error_code} "
                        f"{acc.risk_error_msg}）。"
                        "已把浏览器窗口提到前台、发送系统通知并尝试拉起验证界面，"
                        "请人工完成后重试。"
                    ),
                    "verify_data": acc.verify_data,
                }
            elif acc.gateway_error:
                yield {"type": "error", "data": f"上游鉴权失败：{acc.gateway_error}"}
            elif acc.error_code and not emitted:
                yield {
                    "type": "error",
                    "data": f"上游错误 code={acc.error_code} msg={acc.error_msg}",
                }
            elif not emitted:
                yield {"type": "error", "data": "上游没有返回任何正文（可能触发了风控）"}

            yield {
                "type": "done",
                "data": {
                    "conversation_id": acc.conversation_id,
                    "elapsed": round(time.monotonic() - t0, 2),
                    "read_source": "page-fetch",
                    "model": model_id,
                    "verify_data": acc.verify_data,
                },
            }
        finally:
            self._queues.pop(token, None)
            # 让页面内循环自己收尾（它有超时，不会永久挂）；
            # 但绝不无限等它，否则又变成"页面卡死"。
            if not eval_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(eval_task), 15)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    eval_task.cancel()
                except Exception:  # noqa: BLE001
                    pass
            # 收掉可能的异常，避免 "Task exception was never retrieved"
            if eval_task.done() and not eval_task.cancelled():
                try:
                    eval_task.exception()
                except Exception:  # noqa: BLE001
                    pass


_DRIVER: Optional[ApiDriver] = None


def get_api_driver() -> ApiDriver:
    global _DRIVER
    if _DRIVER is None:
        _DRIVER = ApiDriver()
    return _DRIVER


async def complete_api(model_id: str, text: str, **kw) -> str:
    """一次性拿完整回答（非流式用）。"""
    out: List[str] = []
    err: Optional[str] = None
    async for ev in get_api_driver().stream(model_id, text, **kw):
        if ev["type"] == "delta":
            out.append(ev["data"])
        elif ev["type"] == "error":
            err = ev["data"]
    if not out and err:
        raise RuntimeError(err)
    return "".join(out)
