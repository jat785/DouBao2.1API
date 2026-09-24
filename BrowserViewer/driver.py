"""上游协议层：把豆包网页版的 SSE 流解析成增量文本。

本模块**只做解析**，不含任何浏览器/页面操作。
真正的请求发送在 api_driver.py（页面内 fetch + 实测模板）。

────────────────────────────────────────────────────────────
历史说明（保留，避免重复踩坑）
本文件早期还包含一条「DOM 兜底」路径（轮询页面上最后一条回复的文本）。
它已被删除，原因：
  · 发完消息后，页面会**立刻**把用户的提问渲染出来，DOM 轮询会把它当成
    回复抢先吐出去，于是网络旁路的真实回答永远没机会生效；
  · 一旦弹人机验证，浮层会盖住整个页面（<html> intercepts pointer events），
    DOM 相关的所有操作（输入框、发送、模型选择器）全部点不动。
现在页面交互只保留在 tools/capture_templates.py（录模板用，一次性）。
────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import logging
import re
from typing import List


log = logging.getLogger("BrowserViewer.driver")

# ── prompt 扁平化 ─────────────────────────────────────────
_ROLE_TAG = {"system": "【系统设定】", "user": "【用户】", "assistant": "【此前回复】"}


def flatten_messages(messages: List[dict]) -> str:
    """把 OpenAI messages 压成一段文本。

    网页端没有 system/assistant 通道，只能退化成带角色的纯文本。
    单条 user 消息是最优情况，原样透传不加任何装饰。
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    if len(msgs) == 1 and msgs[0].get("role") == "user":
        return _content_to_text(msgs[0].get("content"))

    parts: List[str] = []
    for m in msgs:
        role = m.get("role", "user")
        text = _content_to_text(m.get("content"))
        if not text:
            continue
        if role == "tool":
            parts.append(f"【工具结果】{text}")
        else:
            parts.append(f"{_ROLE_TAG.get(role, '【用户】')}{text}")
    if len(parts) == 1:
        # 只有一条，去掉装饰
        return re.sub(r"^【[^】]*】", "", parts[0]).strip()
    return "\n\n".join(parts)


def _content_to_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text" and part.get("text"):
                    out.append(str(part["text"]))
                elif part.get("type") == "image_url":
                    out.append("（本服务暂不支持图片输入，已忽略该图片）")
        return "\n".join(out)
    return str(content)


# ── SSE 增量解析器 ────────────────────────────────────────
# 逻辑直接对照上游 SeiShonagon520/doubao2api 的 client.py:1085-1232，
# 那是经过长期实测的版本。**核心是 in_thinking 状态机**：
#
#   block_type == 10040 出现第 1 次 -> 进入思维链
#   block_type == 10040 出现第 2 次 -> 退出思维链
#   退出之后的 block_type == 10000 里的 text_block.text 才是真正的回答
#
# 我第一版把 10040 当"噪声帧丢掉"，没有状态，于是把思维链标题
# （"正在理解任务要求"）当成了回答 —— 这个坑上游代码里已经踩过了。
#
# 另一个要点：文本有三个来源，都要认
#   1. obj["text"]（紧凑 delta）
#   2. patch_op[].patch_value.content_block[].content.text_block.text
#   3. patch_op[].patch_value.content 是 JSON 字符串，里面的 "text" 字段

# ── SSE 解析 ──────────────────────────────────────────────
# 实测帧结构（2026-09-24，模板重放抓到的原始 SSE，www.doubao.com/chat/completion）：
#
#   #0 SSE_HEARTBEAT              data: {}                      心跳，忽略
#   #1 STREAM_TIMEOUT_CONTROL                                   忽略
#   #2 SSE_ACK                    ack_client_meta.conversation_id  ← 会话 id 来源
#   #3 FULL_MSG_NOTIFY            message.content_type=9999      ← **回显用户提问，必须排除**
#                                 message.content_block[].content.text_block.text
#   #5 STREAM_MSG_NOTIFY          block_type=10091 elapsed_block、bot_state 等元信息
#   #6 STREAM_CHUNK               block_type=10040 thinking_block{finish_title,streaming_title}
#   #7 STREAM_CHUNK               block_type=10000 text_block{"text":"收到"}   ← 真正的回答增量
#   #9 STREAM_CHUNK               block_type=10000 text_block{} is_finish=true  ← 同一 block_id 的收尾补丁
#   #11-13 SSE_REPLY_END                                         结束
#
# 因此本前端的正确规则是：
#   1. **不要用 10040 做思维链开关**。这一版里 10040 只是 UI 占位块
#      （content.thinking_block 只有标题，没有正文字段），而且整条流只出现 1 次，
#      按上游"第 1 次进入 / 第 2 次退出"处理会永远卡在 thinking 态，
#      把真正的回答全部丢掉（实测踩过）。
#   2. 正文只认 block_type=10000 且 text_block.text 非空。
#   3. **text_block.text 是增量片段，必须原样累加，绝不能去重**。
#      同一个 block_id 会连续出现多次，每次只带新增的那一小段；
#      曾经误以为是"全量重发"而按 block_id 去重，结果回答缺一大段。
#   4. 回显靠 FULL_MSG_NOTIFY / message.content_type==9999 排除
#      （上游则是靠"只认 CHUNK_DELTA / STREAM_MSG_NOTIFY 等事件名"天然避开）。

_BLOCK_THINKING = 10040
_BLOCK_TEXT = 10000
_ECHO_CONTENT_TYPE = 9999


class SSEAccumulator:
    """有状态的增量解析：喂 SSE 原文，吐出 (thinking, text) 增量。

    以实测帧结构为准（见上方注释），关键设计：
      - 正文 = block_type 10000 的 text_block.text 增量，原样累加（不去重）
      - FULL_MSG_NOTIFY / content_type=9999 一律判为用户回显，丢弃
      - 风控码（710022002/710022004）单独记录，由调用方决定抛
      - 风控 decision 原文也留下 —— 它是拉起验证界面的凭据（见 sel.show_verify）
    """

    RISK_ERROR_CODES = {710022002, 710022004}

    def __init__(self) -> None:
        self._buf = ""
        self._answer_block_id = ""   # 当前回答块（仅用于观察阶段切换，不做去重）
        self.conversation_id = ""
        self.error_code = 0
        self.error_msg = ""
        self.risk_error_code = 0
        self.risk_error_msg = ""
        self.verify_data = ""        # 风控 extra.decision 原文，喂给页面验证 SDK
        self.gateway_error = ""
        self.finished = False

    # ── 事件切分 ──
    @staticmethod
    def split_event_block(block: str):
        """把单个 SSE 事件块切成 (event_name, obj)。"""
        name = ""
        data_lines: List[str] = []
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if not data_lines:
            return name, None
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            return name, "DONE"
        try:
            obj = json.loads(payload)
        except Exception:  # noqa: BLE001
            return name, None
        return name, (obj if isinstance(obj, dict) else None)

    def feed(self, raw: str):
        """喂入原始 SSE 文本，yield (thinking_delta, text_delta)。

        注意：必须先把 \r\n 归一成 \n。上游在 JS 里是按行 split 解析的，
        天然不受换行风格影响；Python 侧如果只认 `\n\n` 分帧，
        遇到 `\r\n\r\n` 会**一帧都切不出来**（实测踩过）。
        """
        if "\r" in raw:
            raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        self._buf += raw
        while "\n\n" in self._buf:
            block, self._buf = self._buf.split("\n\n", 1)
            name, obj = self.split_event_block(block)
            yield from self._handle(name, obj)

    def flush(self):
        """流结束后处理残帧（末尾可能没有空行）。"""
        if self._buf.strip():
            name, obj = self.split_event_block(self._buf)
            self._buf = ""
            yield from self._handle(name, obj)

    # ── 单帧处理 ──
    def _handle(self, name: str, obj):
        if obj is None:
            return
        if obj == "DONE":
            self.finished = True
            return

        if name == "gateway-error":
            self.gateway_error = (
                f"gateway-error: code={obj.get('code','')} message={obj.get('message','')}"
            )
            self.finished = True
            return

        if name == "SSE_ACK":
            ack = obj.get("ack_client_meta")
            if isinstance(ack, dict):
                cid = str(ack.get("conversation_id", "")).strip()
                if cid and cid != "0":
                    self.conversation_id = cid
            return

        if name == "SSE_REPLY_END":
            self.finished = True
            return

        if name == "STREAM_ERROR" or "error_code" in obj:
            code = int(obj.get("error_code", 0) or 0)
            msg = str(obj.get("error_msg", "") or "")
            if code:
                if code in self.RISK_ERROR_CODES:
                    self.risk_error_code = code
                    self.risk_error_msg = msg
                    # extra.decision 是验证凭据；实测形如
                    # {"code":"10000","type":"verify","subtype":"slide","detail":"..."}
                    extra = obj.get("extra")
                    if isinstance(extra, dict):
                        dec = extra.get("decision")
                        if isinstance(dec, str) and dec:
                            self.verify_data = dec
                else:
                    self.error_code = code
                    self.error_msg = msg
                return

        # ── 回显用户提问：整帧丢弃
        msg = obj.get("message")
        if isinstance(msg, dict):
            if msg.get("content_type") == _ECHO_CONTENT_TYPE or "content_block" in msg:
                return
            # 有些帧把正文放在 message 里
            if msg.get("content_type") in (1, 2001, 10000, 2008):
                tb = msg.get("content")
                if isinstance(tb, str) and not tb.lstrip().startswith("[{"):
                    yield ("", tb)
                return

        # ── 路径 1：紧凑 text（CHUNK_DELTA）
        # 对照上游 browser_client.py:1361 —— 只有 CHUNK_DELTA 才直接取顶层 text
        t = obj.get("text")
        if isinstance(t, str) and t and "error_code" not in obj:
            if not name or name == "CHUNK_DELTA":
                yield ("", t)
                return

        # ── 路径 2：content_block，block_type=10000 的 text_block.text
        #
        # ⚠️ 这里是**增量片段**，不是全量重发。
        # 实测（同一 block_id 的连续 patch）：
        #     text='递归是指在求解问题时，将规模'
        #     text='较大的原始问题逐层拆解为结构完全'
        #     text='间接调用自身，直到触达无需再拆解的最小终止条件后，再逐层回溯汇总得到'
        # 每一帧只带**新增的那一小段**，所以必须原样累加。
        # 曾经错误地按 block_id 去重（以为是全量重发），结果把同一个 block_id
        # 的后续片段全部丢掉，症状是回答缺一大段、且夹着"。"这类孤立片段。
        # 上游 browser_client.py:1364-1371 正是无条件 `return tb["text"]`。
        for cb in self._iter_blocks(obj):
            if not isinstance(cb, dict):
                continue
            if cb.get("block_type") != _BLOCK_TEXT:
                # 10040=思维链 UI 占位、10091=计时块，都不是正文
                continue
            content = cb.get("content")
            content = content if isinstance(content, dict) else {}
            tb = content.get("text_block")
            if not isinstance(tb, dict):
                continue
            piece = tb.get("text")
            if not piece:
                continue  # 收尾补丁：text_block 为空（is_finish=true）
            bid = cb.get("block_id") or ""
            # 只把 block_id 记成"当前回答块"，用于识别阶段切换（思维->回答），
            # **不做去重** —— 同一 block_id 的每个 patch 都是新片段。
            if bid and bid != self._answer_block_id:
                if self._answer_block_id:
                    log.debug("回答块切换 %s -> %s", self._answer_block_id, bid)
                self._answer_block_id = bid
            yield ("", piece)

        # ── 路径 3 补充：patch_value.content 是 JSON 字符串
        # 对照上游 browser_client.py:1372 —— **只有 patch_object==102** 才是文本载体，
        # 其它 patch_object 的 content 是元信息（grep 它的实现可确认这一点）。
        for op in obj.get("patch_op") or []:
            if not isinstance(op, dict) or op.get("patch_object") != 102:
                continue
            pv = op.get("patch_value")
            if not isinstance(pv, dict):
                continue
            raw = pv.get("content")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            tt = parsed.get("text") if isinstance(parsed, dict) else None
            if isinstance(tt, str) and tt:
                yield ("", tt)

    @staticmethod
    def _iter_blocks(data: dict):
        for patch in data.get("patch_op") or []:
            if not isinstance(patch, dict):
                continue
            pv = patch.get("patch_value")
            if isinstance(pv, dict):
                yield from pv.get("content_block") or []
        dc = data.get("content")
        if isinstance(dc, dict):
            yield from dc.get("content_block") or []


def parse_sse_events(raw: str) -> List[dict]:
    """把 SSE 原始文本切成 JSON 帧（诊断用；主流程走 SSEAccumulator）。"""
    events: List[dict] = []
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    events.append(json.loads(data))
                except Exception:  # noqa: BLE001
                    continue
    return events

