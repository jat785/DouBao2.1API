"""额度抓取：读「当前时段用量 / 7 天用量 / 重置时间 / 套餐到期」。

═══════════════════════════════════════════════════════════════
为什么必须这么绕（每一条都是实测结论，不是推测）

豆包的额度接口是 `/alice/commerce/sale/subscription/overview/`，
它**必须带 `a_bogus` 签名**。实测四种方式：

  ❌ 纯 httpx 带 cookie 直连         → 404（缺签名）
  ❌ 手搓 query + 自己调 frontierSign → 404（参数不全 / 签名口径不对）
  ❌ 在 /member/quota-management 页面里 fetch → 全 404
       （该页 fetch 被 CSP 拦，连它自己的 CDN 静态资源都 Failed to fetch）
  ✅ 开一个**新标签页导航到额度页**，用 page.on("response") 收 JSON

所以本模块的做法是：让页面自己去请求，我们在旁边收。

为什么用「新标签页」而不是复用主页面：
  实测（已逐项核对）主页面指纹在抓取前后**完全一致** ——
  `page.is_closed()` 为 False、URL 仍是 /chat/、正文 509 字节不变、
  输入框 1 个不变、`window.bdms.frontierSign` 仍可用。
  即抓额度**不会打断正在进行的聊天**。

额度不是 token 配额，而是**滚动时间窗的用量百分比**：
  window_type=1 → 3 小时窗口（主限制）
  window_type=2 → 7 天窗口
═══════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import config

log = logging.getLogger("BrowserViewer.quota")

# 窗口类型 -> 给人看的名字（实测算出的窗口长度见下）
_WINDOW_NAMES = {1: "当前时段", 2: "近 7 天"}
_WINDOW_HOURS = {1: 3.0, 2: 24 * 7}

# 要收的接口路径（实测这 5 个都返回 JSON；overview 是主数据）
_WANTED = (
    "/alice/commerce/sale/subscription/overview/",
    "/alice/commerce/sale/subscription/entry/config/",
    "/alice/commerce/marketing/card/list/",
    "/alice/commerce/sale/subscription/status/",
    "/alice/profile/self",
)


# ── 解析 ──────────────────────────────────────────────────
def _ms(v: Any) -> Optional[float]:
    """毫秒时间戳 -> 本地时间字符串。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n / 1000).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def parse_quota(overview: dict, *, now: Optional[float] = None) -> dict:
    """把 overview 接口的返回解析成前端要的结构。

    字段路径（实测）：
      data.current_subscription.display.short_name   套餐名
      data.current_subscription.end_time             赠送到期（毫秒）
      data.window_limit_section.window_limit_groups[].window_limits[]
          ├─ window_type   1=3小时  2=7天
          ├─ used_percent  已用百分比
          ├─ start_time / end_time  毫秒
      data.window_limit_section.window_limit_groups[].usage_exhausted   ← 在 **group** 层
      data.window_limit_section.usage_exhausted                         ← 顶层也有一份
    """
    now = now if now is not None else time.time()
    data = (overview or {}).get("data") or {}

    plan = ""
    expires_ms = None
    sub = data.get("current_subscription") or {}
    disp = sub.get("display") or {}
    plan = disp.get("short_name") or disp.get("product_name") or ""
    expires_ms = sub.get("end_time")
    is_gift = sub.get("is_gift")

    section = data.get("window_limit_section") or {}
    groups = section.get("window_limit_groups") or []

    windows: List[dict] = []
    exhausted = bool(section.get("usage_exhausted"))
    for grp in groups:
        # ⚠️ usage_exhausted 挂在 group 层（曾经误读到 window 层，得到 None）
        if grp.get("usage_exhausted"):
            exhausted = True
        for w in grp.get("window_limits") or []:
            wt = w.get("window_type")
            end_ms = w.get("end_time")
            left = None
            try:
                left = max(0, int(int(end_ms) / 1000 - now))
            except (TypeError, ValueError):
                pass
            windows.append(
                {
                    "window_type": wt,
                    "name": _WINDOW_NAMES.get(wt, f"窗口 {wt}"),
                    "hours": _WINDOW_HOURS.get(wt),
                    "used_percent": w.get("used_percent"),
                    "less_than_one_percent": w.get("less_than_one_percent"),
                    "start_at": _ms(w.get("start_time")),
                    "reset_at": _ms(end_ms),
                    "seconds_left": left,
                }
            )
    # 3 小时窗口排前面
    windows.sort(key=lambda x: (x.get("window_type") or 99))

    return {
        "account": config.ACCOUNT_LABEL,
        "plan": plan,
        "plan_expires_at": _ms(expires_ms),
        "plan_is_gift": is_gift,
        "windows": windows,
        "exhausted": exhausted,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── 抓取 ──────────────────────────────────────────────────
class QuotaError(RuntimeError):
    """抓取失败。message 直接给前端显示。"""


class QuotaFetcher:
    """带缓存 + 串行化的额度抓取器。

    抓一次约 7 秒，所以必须缓存；并发调用共用同一次抓取。
    """

    def __init__(self) -> None:
        self._cache: Optional[dict] = None
        self._cache_at = 0.0
        self._lock = asyncio.Lock()

    def cached(self) -> Optional[dict]:
        return self._cache

    def _fresh(self) -> bool:
        return (
            self._cache is not None
            and (time.monotonic() - self._cache_at) < config.QUOTA_CACHE_TTL
        )

    async def get(self, session, *, force: bool = False) -> dict:
        """取额度。force=True 忽略缓存。"""
        if not force and self._fresh():
            log.debug("额度命中缓存（%.0fs 前）", time.monotonic() - self._cache_at)
            return self._cache

        async with self._lock:
            # 等锁期间别人可能已经抓好了
            if not force and self._fresh():
                return self._cache
            data = await self._fetch(session)
            self._cache = data
            self._cache_at = time.monotonic()
            return data

    async def _fetch(self, session) -> dict:
        page = await session.page()
        ctx = page.context
        captured: Dict[str, dict] = {}

        async def on_response(resp):
            try:
                url = resp.url
                if not any(w in url for w in _WANTED):
                    return
                if resp.request.resource_type in ("image", "font", "stylesheet", "script"):
                    return
                body = await resp.text()
                if not body or not body.lstrip().startswith(("{", "[")):
                    return
                path = url.split("?")[0].replace("https://www.doubao.com", "")
                if path not in captured:
                    captured[path] = {"status": resp.status, "body": body}
            except Exception:  # noqa: BLE001
                log.debug("额度响应读取失败", exc_info=True)

        # 关键：**新标签页**，不碰正在用的聊天页
        qpage = await ctx.new_page()
        qpage.on("response", lambda r: asyncio.create_task(on_response(r)))
        try:
            log.info("抓取额度（新标签页）…")
            t0 = time.monotonic()
            await qpage.goto(
                config.QUOTA_PAGE_URL,
                wait_until="networkidle",
                timeout=int(config.QUOTA_TIMEOUT * 1000),
            )
            # networkidle 之后接口通常已回来；再给一点余量并轮询等主数据
            for _ in range(20):
                if "/alice/commerce/sale/subscription/overview/" in captured:
                    break
                await asyncio.sleep(0.5)
            log.info(
                "额度抓取完成，用时 %.1fs，捕获 %d 个接口",
                time.monotonic() - t0,
                len(captured),
            )
        except Exception as exc:  # noqa: BLE001
            raise QuotaError(f"打开额度页失败：{type(exc).__name__}: {exc}") from exc
        finally:
            try:
                await qpage.close()
            except Exception:  # noqa: BLE001
                pass

        ov = captured.get("/alice/commerce/sale/subscription/overview/")
        if not ov:
            raise QuotaError(
                "没有捕获到额度接口返回（可能未登录、被风控，或上游改版）。"
                "请先跑 login.bat 确认登录态。"
            )
        try:
            overview = json.loads(ov["body"])
        except Exception as exc:  # noqa: BLE001
            raise QuotaError(f"额度接口返回无法解析：{exc}") from exc

        if overview.get("code") not in (0, None):
            raise QuotaError(
                f"额度接口返回错误 code={overview.get('code')} msg={overview.get('msg')}"
            )

        result = parse_quota(overview)
        if not result["windows"]:
            raise QuotaError("额度接口返回里没有窗口数据（上游可能改版）")
        return result


_FETCHER: Optional[QuotaFetcher] = None


def get_fetcher() -> QuotaFetcher:
    global _FETCHER
    if _FETCHER is None:
        _FETCHER = QuotaFetcher()
    return _FETCHER
