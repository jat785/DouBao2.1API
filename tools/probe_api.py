"""探针：确认「页面内 fetch + 流式回传」这条路是否可用。

要验证四件事：
  1. 页面里 window.bdms.frontierSign 是否存在（签名能力）
  2. 拦截 window.fetch 是否成功
  3. 从页面里 fetch /chat/completion 能不能拿到 SSE 流
  4. 分块回调能不能把数据送回 Python

只读探测：只用模板里的 body，替换一条无害的探测文本。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from BrowserViewer import sel  # noqa: E402
from BrowserViewer.browser import get_session  # noqa: E402

TEMPLATE = Path("templates/doubao-2.1-lite.json")


async def main() -> int:
    sess = get_session()
    page = await sess.page()
    page.set_default_timeout(10000)
    print("页面:", page.url, flush=True)

    if await sel.detect_captcha(page):
        print("!! 当前有人机验证，请先点掉", flush=True)
        await sel.raise_browser_window(page)
        return 1

    print("\n=== 1. 签名能力探测 ===", flush=True)
    probe = await page.evaluate(
        """() => {
            const r = { hasBdms: typeof window.bdms !== 'undefined',
                        hasFrontierSign: !!(window.bdms && typeof window.bdms.frontierSign === 'function'),
                        fetchNative: window.fetch.toString().includes('native code'),
                        ua: navigator.userAgent.slice(0, 60) };
            if (r.hasFrontierSign) {
                try { r.signSample = window.bdms.frontierSign('aid=497858'); }
                catch (e) { r.signError = String(e); }
            }
            return r;
        }"""
    )
    print(json.dumps(probe, ensure_ascii=False, indent=1), flush=True)

    print("\n=== 2. 安装 fetch 拦截器 ===", flush=True)
    await page.evaluate(
        """() => {
            if (window.__dshHooked) return 'already';
            window.__dshOriginalFetch = window.fetch;
            window.__dshHooked = true;
            return 'ok';
        }"""
    )
    print("hook 安装完成", flush=True)

    print("\n=== 3. 在页面内发起 /chat/completion ===", flush=True)
    tpl = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    body = tpl["body_json"]
    # 替换探测文本，其余原样（模板来自真实请求，字段最稳）
    body["messages"][0]["content_block"][0]["content"]["text_block"]["text"] = "只回复两个字：收到"
    url = tpl["url"]

    result = await page.evaluate(
        """async ([url, bodyStr]) => {
            const chunks = [];
            try {
                const res = await window.__dshOriginalFetch(url, {
                    method: 'POST',
                    headers: { 'content-type': 'application/json', 'accept': 'text/event-stream' },
                    body: bodyStr,
                    credentials: 'include',
                });
                const info = { status: res.status, ct: res.headers.get('content-type') };
                if (!res.body) return { ...info, error: 'no body stream' };
                const reader = res.body.getReader();
                const dec = new TextDecoder('utf-8');
                let total = 0;
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    const t = dec.decode(value, { stream: true });
                    total += t.length;
                    if (chunks.length < 40) chunks.push(t);
                    if (total > 60000) break;
                }
                return { ...info, total, chunks };
            } catch (e) {
                return { error: String(e) };
            }
        }""",
        [url, json.dumps(body, ensure_ascii=False)],
    )

    if result.get("error"):
        print("!! 失败:", result["error"], flush=True)
        return 1

    print(f"status={result.get('status')} content-type={result.get('ct')} 总字节={result.get('total')}", flush=True)

    from BrowserViewer.driver import SSEAccumulator

    acc = SSEAccumulator()
    text = ""
    for chunk in result.get("chunks", []):
        for _th, tx in acc.feed(chunk):
            if tx:
                text += tx
    for _th, tx in acc.flush():
        if tx:
            text += tx

    print("\n=== 4. 状态机抽取结果 ===", flush=True)
    print("conversation_id:", acc.conversation_id, flush=True)
    print("error_code     :", acc.error_code, acc.error_msg, flush=True)
    print("risk_error     :", acc.risk_error_code, acc.risk_error_msg, flush=True)
    print("gateway_error  :", acc.gateway_error, flush=True)
    print("抽取正文       :", repr(text), flush=True)

    # 还原 hook，避免影响页面自身
    await page.evaluate("() => { if (window.__dshOriginalFetch) { window.fetch = window.__dshOriginalFetch; window.__dshHooked = false; } }")
    await sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
