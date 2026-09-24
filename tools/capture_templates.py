"""抓取「豆包工作」三个模型的真实请求模板。

为什么要这个：
    上游项目 SeiShonagon520/doubao2api 的浏览器层**完全不点界面** ——
    它在 page.evaluate 里自己构造 JSON、自己 fetch、自己读 SSE，
    只有 a_bogus 签名借页面算（window.bdms.frontierSign）。
    本项目照这个架构走，但请求体字段太多没法凭猜写，所以：
      → 先用界面点一次，把真实请求原样录下来 → 之后全部复用模板，不再碰界面。

用法：
    capture.bat            # 或 .venv\\Scripts\\python.exe -m tools.capture_templates
需要你先登录（脚本会等你）。过程中若弹人机验证，脚本会把窗口拉到前台并等你点掉。

产出：templates/<model>.json，每个文件含
    url / method / headers / body（脱敏只去 cookie，其它原样）
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from BrowserViewer import config, sel  # noqa: E402
from BrowserViewer.browser import get_session  # noqa: E402
from BrowserViewer.registry import MODELS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s | %(message)s")
log = logging.getLogger("tools.capture")

OUT_DIR = Path("templates")
PROBE_TEXT = "只回复两个字：收到"

# 要抓的模型（对应界面上的三个档位）
TARGETS = ["doubao-2.1-lite", "doubao-2.1-turbo", "doubao-2.1-pro"]


async def wait_captcha_clear(page, *, timeout: float = 300) -> bool:
    hit = await sel.detect_captcha(page)
    if not hit:
        return True
    print("\n" + "=" * 68, flush=True)
    print(f"  检测到人机验证：{hit}", flush=True)
    print("  已把浏览器窗口提到前台，请人工完成图片验证。", flush=True)
    print("  完成后脚本自动继续（最多等 5 分钟）。", flush=True)
    print("=" * 68, flush=True)
    await sel.raise_browser_window(page)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        if not await sel.detect_captcha(page):
            print("[✓] 验证已通过，继续。", flush=True)
            return True
    print("[✗] 等待验证超时。", flush=True)
    return False


async def capture_one(page, mid: str) -> dict | None:
    """选中一个模型，发一条探测消息，录下 /chat/completion 请求。"""
    spec = MODELS[mid]
    print(f"\n---- 抓取 {mid}（界面文案 {spec.ui_label}）----", flush=True)

    if not await wait_captcha_clear(page):
        return None

    await sel.switch_mode(page, "work")
    ok = await sel.select_model(page, spec)
    back = (await sel.current_model_text(page)).strip().replace("\n", " | ")
    print(f"  切模型: {ok} | 按钮回读 {back!r}", flush=True)
    if not ok:
        print("  [×] 模型没切成功，跳过（可稍后重跑）", flush=True)
        return None

    captured: dict = {}
    done = asyncio.Event()

    def on_request(req):
        if "/chat/completion" not in req.url or done.is_set():
            return
        try:
            body = req.post_data
        except Exception:  # noqa: BLE001
            body = None
        if not body:
            return
        captured["url"] = req.url
        captured["method"] = req.method
        captured["headers"] = dict(req.headers)
        captured["body"] = body
        done.set()

    page.on("request", on_request)
    try:
        await sel.fill_input(page, PROBE_TEXT)
        await sel.submit(page)
        try:
            await asyncio.wait_for(done.wait(), 60)
        except asyncio.TimeoutError:
            print("  [×] 60 秒内没抓到请求（可能又是验证）", flush=True)
            await wait_captcha_clear(page)
            return None
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:  # noqa: BLE001
            pass

    # 把 cookie 从 headers 里去掉（不落盘敏感信息）
    headers = captured.get("headers", {})
    captured["headers"] = {k: v for k, v in headers.items() if k.lower() != "cookie"}

    # 解析 body，方便后面做模板
    try:
        parsed = json.loads(captured["body"])
        captured["body_json"] = parsed
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] body 不是 JSON: {exc}", flush=True)

    print(f"  抓到 URL: {captured['url'].split('?')[0]}", flush=True)
    print(f"  抓到 body: {len(captured.get('body',''))} 字节", flush=True)
    return captured


async def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    sess = get_session()
    page = await sess.page()
    page.set_default_timeout(10000)
    print(f"页面: {page.url}", flush=True)

    if await sel.needs_login(page):
        print("\n需要登录：请在弹出的浏览器窗口扫码（最多等 10 分钟）", flush=True)
        if not await sess.ensure_login(wait_for_user=True):
            print("登录超时", flush=True)
            await sess.close()
            return 1

    results = {}
    for mid in TARGETS:
        cap = await capture_one(page, mid)
        if cap:
            f = OUT_DIR / f"{mid}.json"
            f.write_text(json.dumps(cap, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  [✓] 已保存 {f}", flush=True)
            results[mid] = True
        else:
            results[mid] = False
        await asyncio.sleep(3)

    print("\n" + "=" * 68, flush=True)
    for mid, ok in results.items():
        print(f"  {'[✓]' if ok else '[×]'} {mid}", flush=True)
    print("=" * 68, flush=True)

    await sess.close()
    return 0 if all(results.values()) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
