"""浏览器会话：Playwright 持久化上下文 + 单例 + 串行锁。

为什么用浏览器而不是直接 HTTP 调豆包接口：
    豆包网页端有 a_bogus / msToken 之类的签名与风控。用浏览器意味着这些
    签名由页面自己的 JS 生成，我们完全不碰 —— 这是一大块可以永远不做的工作。
    代价是慢和只能串行，由这里的锁保证。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger("BrowserViewer.browser")


def resolve_executable() -> Optional[str]:
    """挑一个浏览器可执行文件。

    优先本机 Chrome：指纹最像真实用户，也不用等 Playwright 下载 headless-shell
    （国内网络下这个下载经常失败）。找不到就返回 None，交给 Playwright 自带 chromium。
    """
    import os

    candidates = []
    if config.BROWSER_EXECUTABLE:
        candidates.append(config.BROWSER_EXECUTABLE)

    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates += [
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


class BrowserSession:
    """全局单例。整个服务共用一个 Chromium + 一个页面。

    ── 多账号（只预留接口，未实现切换）────────────────────
    浏览器会话绑定到「当前账号」的 user-data-dir（`config.USER_DATA_DIR`）
    + `config.ACCOUNT_LABEL`（仅用于日志/展示）。

    切换账号的方式：换掉 `DOUBAO_USER_DATA_DIR` / `DOUBAO_ACCOUNT_LABEL`
    然后重启服务。因为**同一时刻只跑一个 Chrome**（你的策略是一个号
    完全用完再切下一个，而不是反复切换），所以不需要多实例、不需要
    账号注册表、也不需要按账号加锁。

    真要做多账号时，这里需要改成「账号 -> BrowserSession」的注册表，
    并把 server 的全局 `_SERIAL` 换成按账号的锁。**现在不做。**
    """

    def __init__(self) -> None:
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()      # 串行化上游请求
        self._start_lock = asyncio.Lock()
        self._last_used = 0.0
        self._attached = False           # True = 附加到别人的浏览器，不能关掉它

    # ── 生命周期 ────────────────────────────────────────
    async def _try_attach(self):
        """尝试附加到已经在跑的、带调试端口的浏览器。

        原因：Chrome 的 user-data-dir 同一时刻只能被一个进程打开。
        服务在跑的时候，调试脚本/第二个实例不能再启一个，否则会直接退出。
        所以优先「附加」，失败才「自己启」。
        """
        if not config.CDP_PORT:
            return None
        import httpx

        from playwright.async_api import async_playwright

        endpoint = f"http://127.0.0.1:{config.CDP_PORT}"
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(f"{endpoint}/json/version")
                if r.status_code != 200:
                    return None
                log.info("发现已有浏览器（CDP %s），附加中…", config.CDP_PORT)
        except Exception:  # noqa: BLE001
            return None

        try:
            self._pw = await async_playwright().start()
            browser = await self._pw.chromium.connect_over_cdp(endpoint)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.set_default_timeout(12000)
            self._ctx = ctx
            self._page = page
            self._attached = True
            log.info("已附加到现有浏览器，页面=%s", page.url)
            return page
        except Exception:  # noqa: BLE001
            log.debug("附加失败，改为自行启动", exc_info=True)
            try:
                if self._pw is not None:
                    await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
            return None

    async def _ensure(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        async with self._start_lock:
            if self._page is not None and not self._page.is_closed():
                return self._page

            attached = await self._try_attach()
            if attached is not None:
                return attached

            from playwright.async_api import async_playwright

            user_data_dir = Path(config.USER_DATA_DIR)
            user_data_dir.mkdir(parents=True, exist_ok=True)

            exe = resolve_executable()
            log.info(
                "启动浏览器 (账号=%s, headless=%s, profile=%s, exe=%s, cdp=%s)",
                config.ACCOUNT_LABEL,
                config.HEADLESS,
                user_data_dir,
                exe or "playwright-bundled-chromium",
                config.CDP_PORT or "-",
            )
            self._pw = await async_playwright().start()

            args = [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if config.CDP_PORT:
                args.append(f"--remote-debugging-port={config.CDP_PORT}")

            launch_kwargs = {
                "user_data_dir": str(user_data_dir),
                "headless": config.HEADLESS,
                "args": args,
                "ignore_default_args": ["--enable-automation"],
                "viewport": {"width": 1440, "height": 900},
                "locale": "zh-CN",
            }
            if exe:
                launch_kwargs["executable_path"] = exe
            if config.PROXY:
                launch_kwargs["proxy"] = {"server": config.PROXY}

            try:
                self._ctx = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "existing browser session" in msg or "ProcessSingleton" in msg:
                    raise RuntimeError(
                        "这个 profile 已被另一个进程占用（通常是已经有一个 "
                        "DouBao2.1API 服务在跑）。\n"
                        f"解决办法：确认 DOUBAO_CDP_PORT={config.CDP_PORT} 在两边一致，"
                        "先启动的那个会持有浏览器，后启动的会自动附加；\n"
                        "或者干脆只跑一个实例。"
                    ) from exc
                raise

            # 去掉最明显的自动化指纹
            await self._ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )

            pages = self._ctx.pages
            self._page = pages[0] if pages else await self._ctx.new_page()
            # 默认超时别太大：一次误点会白等很久。具体操作各自再传更细的超时。
            self._page.set_default_timeout(12000)

            await self._page.goto(config.CHAT_URL, wait_until="domcontentloaded")
            log.info("已打开 %s，当前标题=%r", config.CHAT_URL, await self._page.title())
            return self._page

    async def page(self):
        return await self._ensure()

    async def close(self) -> None:
        # 附加模式下不能关掉别人的浏览器，只断开自己的连接
        if self._attached:
            try:
                if self._pw is not None:
                    await self._pw.stop()
            except Exception:  # noqa: BLE001
                log.debug("断开 CDP 连接失败", exc_info=True)
            self._ctx = self._page = self._pw = None
            self._attached = False
            return

        try:
            if self._ctx is not None:
                await self._ctx.close()
        except Exception:  # noqa: BLE001
            log.debug("关闭 context 失败", exc_info=True)
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            log.debug("停止 playwright 失败", exc_info=True)
        self._ctx = self._page = self._pw = None

    # ── 串行执行 ────────────────────────────────────────
    async def run_serialized(self, coro_factory, *, min_interval: Optional[float] = None):
        """同一时刻只允许一个上游请求；并保证两次请求之间的最小间隔。"""
        import time

        interval = config.MIN_INTERVAL if min_interval is None else min_interval
        async with self._lock:
            wait = interval - (time.monotonic() - self._last_used)
            if self._last_used and wait > 0:
                await asyncio.sleep(wait)
            try:
                return await coro_factory()
            finally:
                self._last_used = time.monotonic()

    # ── 登录态 ──────────────────────────────────────────
    async def ensure_login(self, *, wait_for_user: bool = True) -> bool:
        """返回是否已登录。未登录且有头模式下会提示人工扫码。"""
        from . import sel

        page = await self._ensure()
        if await sel.is_logged_in(page):
            return True
        log.warning("未检测到登录态。请在弹出的浏览器窗口里完成扫码登录。")
        if not wait_for_user:
            return False
        deadline = 300  # 最多等 5 分钟
        for _ in range(deadline):
            await asyncio.sleep(1)
            if await sel.is_logged_in(page):
                log.info("登录成功，已持久化到 %s", config.USER_DATA_DIR)
                return True
        log.error("等待登录超时")
        return False


_SESSION: Optional[BrowserSession] = None


def get_session() -> BrowserSession:
    global _SESSION
    if _SESSION is None:
        _SESSION = BrowserSession()
    return _SESSION
