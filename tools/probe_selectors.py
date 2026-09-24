"""一次性校准工具：把真实页面的选择器和上游请求字段读出来。

用法（在项目根目录，用 venv 的 python）：
    .venv\\Scripts\\python.exe -m tools.probe_selectors

它会：
  1. 打开持久化浏览器，等你登录（如果需要）
  2. 枚举候选选择器的命中情况
  3. 逐个点选「豆包 2.1 Pro / Turbo / Lite」，读回按钮文案
  4. 打印上游 chat/completion 请求体里与模型相关的字段（已脱敏）

拿到结果后回填 BrowserViewer/registry.py 里的 ui_label / model_id / bot_id。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from BrowserViewer import config, sel  # noqa: E402
from BrowserViewer.browser import get_session  # noqa: E402
from BrowserViewer.registry import MODELS  # noqa: E402

SENSITIVE = re.compile(r"(cookie|authorization|token|session|sign|xsrf|csrf)", re.I)


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if SENSITIVE.search(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 200:
        return obj[:200] + "…"
    return obj


async def probe(page) -> None:
    print("\n" + "=" * 70)
    print("1) 选择器命中情况")
    print("=" * 70)

    pools = {
        "输入框": sel.INPUT_CANDIDATES,
        "发送按钮": sel.SEND_CANDIDATES,
        "消息项": sel.MESSAGE_ITEM_CANDIDATES,
        "回复内容": sel.REPLY_CONTENT_CANDIDATES,
        "模型选择器": sel.MODEL_BUTTON_CANDIDATES,
        "推理强度": sel.REASONING_BUTTON_CANDIDATES,
    }
    for name, cands in pools.items():
        print(f"\n-- {name}")
        for css in cands:
            try:
                n = await page.locator(css).count()
                visible = 0
                for i in range(min(n, 5)):
                    try:
                        if await page.locator(css).nth(i).is_visible():
                            visible += 1
                    except Exception:  # noqa: BLE001
                        pass
                mark = "OK " if visible else "   "
                print(f"   {mark}{css}  (count={n}, visible={visible})")
            except Exception as exc:  # noqa: BLE001
                print(f"      {css}  -> 无效: {exc}")

    print("\n" + "=" * 70)
    print("2) 模型选择器：点开并枚举所有可见选项")
    print("=" * 70)
    try:
        await sel.open_model_menu(page)
        options = await page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll("*").forEach(el => {
                    if (el.children.length > 3) return;
                    const t = (el.innerText || '').trim();
                    if (!t || t.length > 40 || t.includes('\\n')) return;
                    if (!/2\\.1|自动|Lite|Turbo|Pro/i.test(t)) return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.height < 10) return;
                    const key = t + '@' + Math.round(r.top);
                    if (seen.has(key)) return;
                    seen.add(key);
                    out.push({text: t, top: Math.round(r.top), left: Math.round(r.left),
                              cls: el.className && el.className.toString().slice(0, 80)});
                });
                return out.sort((a,b) => a.top - b.top);
            }"""
        )
        for o in options:
            print(f"   top={o['top']:<5} left={o['left']:<5} {o['text']!r}")
            if o.get("cls"):
                print(f"        class={o['cls']}")
        await page.keyboard.press("Escape")
    except Exception as exc:  # noqa: BLE001
        print(f"   打不开模型菜单: {exc}")

    print("\n" + "=" * 70)
    print("3) 逐个切换模型并读回按钮文案")
    print("=" * 70)
    for spec in MODELS.values():
        ok = await sel.select_model(page, spec)
        now = await sel.current_model_text(page)
        print(f"   {spec.id:<20} 目标={spec.ui_label!r:<22} 回读={now!r:<28} 确认={'是' if ok else '否'}")

    print("\n" + "=" * 70)
    print("4) 抓一次上游请求：模型相关字段（已脱敏）")
    print("=" * 70)
    captured: list = []

    def on_request(req) -> None:
        url = req.url
        if not any(k in url for k in ("chat/completion", "/completion")):
            return
        try:
            body = req.post_data
        except Exception:  # noqa: BLE001
            body = None
        entry = {"url": url.split("?")[0], "method": req.method}
        if body:
            try:
                parsed = json.loads(body)
                entry["body"] = redact(parsed)
            except Exception:  # noqa: BLE001
                entry["body_raw"] = redact(body)
        captured.append(entry)

    page.on("request", on_request)
    try:
        await sel.fill_input(page, "只回复两个字：收到")
        await page.wait_for_timeout(200)
        await sel.submit(page)
        print("   已发送探测消息，等待上游请求…")
        for _ in range(60):
            await asyncio.sleep(1)
            if captured:
                break
        for c in captured[:3]:
            print(f"\n   URL : {c['url']}")
            print(f"   方法: {c['method']}")
            body = c.get("body")
            if isinstance(body, dict):
                for key in ("model", "bot_id", "model_id", "need_deep_think",
                            "reasoning_effort", "use_deep_think"):
                    if key in body:
                        print(f"   {key} = {json.dumps(body[key], ensure_ascii=False)}")
                messages = body.get("messages")
                if messages:
                    print(f"   messages[0] keys = {list(messages[0].keys())}")
                inner = body.get("message") or body.get("payload")
                if isinstance(inner, dict):
                    for key in ("model", "bot_id", "model_id", "need_deep_think"):
                        if key in inner:
                            print(f"   inner.{key} = {json.dumps(inner[key], ensure_ascii=False)}")
                print("   --- 完整 body（脱敏后，前 1200 字）---")
                print("   " + json.dumps(body, ensure_ascii=False)[:1200])
            elif "body_raw" in c:
                print("   raw: " + str(c["body_raw"])[:800])
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:  # noqa: BLE001
            pass


async def main() -> int:
    logging_ok = __import__("logging")
    logging_ok.basicConfig(level=logging_ok.INFO, format="%(levelname)-7s %(name)s | %(message)s")

    sess = get_session()
    page = await sess.page()
    print(f"当前页面: {page.url}")
    print(f"标题    : {await page.title()}")

    if not await sel.is_logged_in(page):
        print("\n*** 未登录。请在弹出的浏览器窗口里扫码登录，登录后会自动继续（最多等 5 分钟）***")
        if not await sess.ensure_login(wait_for_user=True):
            print("登录超时，退出。")
            await sess.close()
            return 1

    try:
        await probe(page)
    finally:
        print("\n探测完成。浏览器保持打开 10 秒后关闭…")
        await asyncio.sleep(10)
        await sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
