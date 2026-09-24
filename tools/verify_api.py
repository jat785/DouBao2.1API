"""端到端验证 API-first 驱动：三个模型各问一次，确认拿到真实回答。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from BrowserViewer.api_driver import get_api_driver  # noqa: E402
from BrowserViewer.browser import get_session  # noqa: E402


async def main() -> int:
    sess = get_session()
    page = await sess.page()
    drv = get_api_driver()
    q = "1+1等于几？只回答数字"
    fails = 0

    for mid in ("doubao-2.1-lite", "doubao-2.1-turbo", "doubao-2.1-pro"):
        print(f"\n{'='*56}\n{mid}\n{'='*56}", flush=True)
        got, err = "", ""
        try:
            async for ev in drv.stream(mid, q, session=sess):
                t = ev["type"]
                if t == "delta":
                    got += ev["data"]
                elif t == "error":
                    err = ev["data"]
                elif t == "done":
                    print(f"  done: {ev['data']}", flush=True)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        print(f"  回答: {got!r}", flush=True)
        if err:
            print(f"  错误: {err}", flush=True)
            fails += 1
        elif not got:
            print("  错误: 空回答", flush=True)
            fails += 1
        await asyncio.sleep(1)

    print(f"\n结果: {'全部通过' if fails == 0 else f'{fails} 个失败'}", flush=True)
    await sess.close()
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
