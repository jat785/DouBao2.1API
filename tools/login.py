"""登录 + 处理人机验证 + 自检，一条命令走完。

用法：
    login.bat
    :: 或
    .venv\\Scripts\\python.exe -m tools.login

流程：
  1. 打开浏览器（有头），停在豆包页面
  2. 未登录就等你扫码（自动检测，最多 10 分钟，不用按键）
  3. 若页面已有验证浮层，把窗口提到前台、发系统通知，等你处理完
  4. 自检（**不走界面点击，用 API 驱动的真实请求**）：
       - 三个模型的请求模板是否齐全
       - 三次真实调用能否拿到回答
  5. 结果写进 probe_report.json

为什么不再做「点下拉切模型」那种界面自检：
    新架构是模板重放（请求体里换 model_item_key），根本不点模型选择器。
    而且点界面一旦遇上验证浮层就会全部失败（<html> intercepts pointer events），
    还会因为反复发消息把账号打进限流。
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
from BrowserViewer.api_driver import get_api_driver  # noqa: E402
from BrowserViewer.browser import get_session  # noqa: E402
from BrowserViewer.registry import MODELS, missing_templates  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s | %(message)s")
log = logging.getLogger("tools.login")

REPORT = Path("probe_report.json")
PROBE_Q = "只回复两个字：可以"


async def wait_for_login(page) -> bool:
    if not await sel.needs_login(page):
        print("\n[✓] 已是登录态，跳过登录步骤。", flush=True)
        return True

    print("\n" + "=" * 68, flush=True)
    print("  需要登录", flush=True)
    print("=" * 68, flush=True)
    print("  浏览器窗口已经打开。请在里面用手机豆包 App 扫码登录。", flush=True)
    print("  检测到登录成功会自动继续（最多等 10 分钟，不用按任何键）。", flush=True)
    print("=" * 68, flush=True)

    deadline = time.monotonic() + 600
    last_tick = 0
    while time.monotonic() < deadline:
        if not await sel.needs_login(page):
            break
        waited = int(600 - (deadline - time.monotonic()))
        if waited // 15 > last_tick:
            last_tick = waited // 15
            print(f"  …等待登录中（已等 {waited}s）", flush=True)
        await asyncio.sleep(1)

    if await sel.needs_login(page):
        print("\n[✗] 仍然检测到「登录」按钮，可能没登录成功。", flush=True)
        return False
    print("\n[✓] 登录成功，已写入 profile：", config.USER_DATA_DIR, flush=True)
    try:
        await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    return True


async def wait_for_captcha(page) -> bool:
    """页面若挂着验证浮层，拉到前台 + 通知，等用户处理完。"""
    hit = await sel.detect_captcha(page)
    if not hit:
        return True
    print("\n" + "=" * 68, flush=True)
    print(f"  检测到人机验证：{hit}", flush=True)
    print("  已把浏览器窗口提到前台，请人工完成验证（滑块 / 短信验证码）。", flush=True)
    print("  完成后本脚本自动继续（最多等 5 分钟）。", flush=True)
    print("=" * 68, flush=True)
    await sel.raise_browser_window(page)
    sel.notify_user("豆包网关：需要人工验证", "请在浏览器窗口完成人机验证。")

    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        if not await sel.detect_captcha(page):
            print("[✓] 验证已通过，继续。", flush=True)
            return True
    print("[✗] 等待验证超时（可稍后重新运行 login.bat）。", flush=True)
    return False


async def run_checks(sess, page) -> dict:
    report: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": []}

    def rec(name: str, ok: bool, detail=""):
        report["steps"].append({"name": name, "ok": ok, "detail": detail})
        print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""), flush=True)

    print("\n" + "=" * 68)
    print("  自检（走 API 驱动，不点界面）")
    print("=" * 68)

    # 0) 已登录？
    rec("登录态", await sel.is_logged_in(page))

    # 1) 模板齐全？
    miss = missing_templates()
    rec("请求模板齐全", not miss,
        "" if not miss else f"缺少 {miss}，请运行 capture.bat")

    # 2) 三个模型各真实调用一次
    drv = get_api_driver()
    report["probe"] = {}
    for mid in MODELS:
        got, err = "", ""
        t0 = time.monotonic()
        try:
            async for ev in drv.stream(mid, PROBE_Q, session=sess):
                if ev["type"] == "delta":
                    got += ev["data"]
                elif ev["type"] == "error":
                    err = ev["data"]
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        el = round(time.monotonic() - t0, 1)
        report["probe"][mid] = {"reply": got, "error": err, "elapsed": el}
        ok = bool(got.strip()) and not err
        rec(f"调用 {mid}", ok, f"{el}s 回答={got.strip()[:40]!r}" + (f" 错误={err[:80]}" if err else ""))
        if err and "UPSTREAM_CAPTCHA" in err:
            print("      -> 命中风控，等你去浏览器处理；其余模型跳过。", flush=True)
            break
        await asyncio.sleep(config.MIN_INTERVAL)

    return report


async def main() -> int:
    sess = get_session()
    page = await sess.page()
    print(f"页面: {page.url}")
    print(f"标题: {await page.title()}")
    print(f"浏览器: {config.USER_DATA_DIR}")

    try:
        if not await wait_for_login(page):
            print("\n登录未完成。修好后重新运行 login.bat。")
            return 1
        await wait_for_captcha(page)
        report = await run_checks(sess, page)
    finally:
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {REPORT.resolve()}")
        await sess.close()

    bad = [s for s in report["steps"] if not s["ok"]]
    print("\n" + "=" * 68)
    if bad:
        print(f"  结果：{len(report['steps']) - len(bad)}/{len(report['steps'])} 通过")
        for s in bad:
            print(f"    ✗ {s['name']}  {s['detail']}")
    else:
        print("  结果：全部通过  可以 start.bat 起服务了")
    print("=" * 68)
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
