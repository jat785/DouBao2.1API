"""DOM 交互层：所有「跟页面长什么样有关」的知识都关在这个文件里。

页面改版时只需要改这里。选择器一律走「候选池 + 首个可见」策略，
并且优先用语义匹配（placeholder / aria-label / 文案），不依赖 hash 类名。

实测校正入口：python -m tools.probe_selectors
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import List, Optional

from .registry import ModelSpec

log = logging.getLogger("BrowserViewer.sel")


# ── 候选池 ────────────────────────────────────────────────
INPUT_CANDIDATES = [
    "textarea[placeholder]",
    "textarea",
    "div[contenteditable='true']",
    "[data-testid='chat-input'] textarea",
    "[class*='editor'][contenteditable='true']",
    "[class*='input'][contenteditable='true']",
    "[role='textbox']",
]

SEND_CANDIDATES = [
    "[data-testid='chat-send-button']",
    "button[aria-label*='发送']",
    "button:has-text('发送')",
    "[class*='send-btn']",
    "[class*='send-button']",
    "[class*='send'][role='button']",
    "button[class*='send']",
]

MESSAGE_ITEM_CANDIDATES = [
    "[class*='inner-item']",
    "[data-testid='message-item']",
    "[class*='message-item']",
    "[class*='chat-item']",
    "[class*='message'][class*='item']",
]

REPLY_CONTENT_CANDIDATES = [
    "[class*='message-content']",
    "[class*='markdown']",
    "[class*='content'][class*='text']",
    "[class*='receive']",
    "[class*='answer']",
    "[class*='bubble']",
]

MODEL_BUTTON_CANDIDATES = [
    # 实测确认（www.doubao.com/chat/）：data-testid 稳定，优先用
    "[data-testid='chat_input_action_model']",
    "[data-testid='model-selector']",
    "button:has-text('2.1')",
    "[class*='model-select']",
    "[class*='model-switch']",
]

# 模型下拉项：实测带 role='menuitem'，文案形如「豆包 快速」「豆包 2.1 Turbo」
MODEL_OPTION_CANDIDATES = [
    "[role='menuitem']",
    "[role='option']",
    "[class*='dropdown'] [role='menuitem']",
    "[class*='menu'] [role='menuitem']",
]

# 「对话 / 工作」模式切换（工作模式会解锁更多模型档位）
WORK_MODE_CANDIDATES = [
    "button:has-text('工作')",
    "[role='tab']:has-text('工作')",
    "[data-testid*='work']",
]

REASONING_BUTTON_CANDIDATES = [
    "button:has-text('推理强度')",
    "[class*='reasoning']",
    "[class*='effort']",
]

LOGIN_MARKERS = [
    "text=登录",
    "text=扫码登录",
    "text=立即登录",
    "[class*='login-panel']",
]

BUSY_MARKERS = [
    "[class*='stop']",
    "button[aria-label*='停止']",
    "[class*='generating']",
    "[class*='loading']",
]


# ── 通用工具 ──────────────────────────────────────────────
async def first_visible(page, candidates: List[str], *, timeout_each: float = 0.6):
    """按候选池顺序返回第一个可见元素；都没有则 None。"""
    for css in candidates:
        try:
            loc = page.locator(css).last
            await loc.wait_for(state="visible", timeout=timeout_each * 1000)
            return loc
        except Exception:  # noqa: BLE001
            continue
    return None


async def first_visible_with_text(page, texts: List[str], *, timeout_each: float = 0.6):
    """按文案匹配可见元素（大小写/空格不敏感）。"""
    for raw in texts:
        needle = raw.strip()
        if not needle:
            continue
        try:
            loc = page.get_by_text(needle, exact=False).last
            await loc.wait_for(state="visible", timeout=timeout_each * 1000)
            return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def normalize(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


# ── 页签：对话 / 工作 ─────────────────────────────────────
# 实测：模型下拉的内容随页签变化。
#   「对话」页签只有 豆包快速 / 豆包 2.1 Turbo 两项
#   「工作」页签才有 自动 / 2.1 Lite / 2.1 Turbo / 2.1 Pro / 推理强度
# 所以必须在「工作」页签下工作。
MODE_TAB_NAMES = {
    "work": ["工作"],
    "chat": ["对话"],
}


async def _current_mode(page) -> str:
    """通过模型下拉的内容判断当前在哪个页签（比看 tab 高亮更可靠）。"""
    try:
        opts = await list_model_options(page)
    except Exception:  # noqa: BLE001
        return ""
    joined = " ".join(opts)
    if "2.1 Pro" in joined or "推理强度" in joined or "Lite" in joined:
        return "work"
    if "快速" in joined:
        return "chat"
    return ""


async def switch_mode(page, mode: str = "work") -> bool:
    """切到「工作」/「对话」页签。返回是否切换成功。"""
    names = MODE_TAB_NAMES.get(mode, ["工作"])
    if await _current_mode(page) == mode:
        log.info("已经在 %s 模式", mode)
        return True

    for name in names:
        try:
            tab = page.get_by_text(name, exact=True).first
            await tab.wait_for(state="visible", timeout=2000)
            await tab.click()
            await page.wait_for_timeout(2500)
            if await _current_mode(page) == mode:
                log.info("已切到 %s 模式（点 %r）", mode, name)
                return True
        except Exception:  # noqa: BLE001
            continue
    log.warning("切到 %s 模式失败", mode)
    return False


# ── 登录态 ────────────────────────────────────────────────
LOGIN_BUTTON_CANDIDATES = [
    "button:has-text('登录')",
    "text=立即登录",
    "text=扫码登录",
    "[class*='login-btn']",
]


async def needs_login(page) -> bool:
    """页面还挂着「登录」按钮 => 未登录。

    注意：**未登录也能看到输入框、也能用「工作」页签浏览模型列表**，
    但**切换模型不会生效**（实测点击后按钮文案不变）。
    所以登录态必须单独判断，不能靠「有没有输入框」。
    """
    for css in LOGIN_BUTTON_CANDIDATES:
        try:
            loc = page.locator(css).first
            if await loc.is_visible(timeout=800):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def is_logged_in(page) -> bool:
    """已登录 = 有输入框 且 没有登录按钮。"""
    box = await first_visible(page, INPUT_CANDIDATES, timeout_each=1.0)
    if box is None:
        return False
    return not await needs_login(page)


# ── 输入框 ────────────────────────────────────────────────
# 实测坑：placeholder 会随状态变（「发消息...」/「发消息或按住空格说话...」/
# 「发消息或创建任务... / 使用技能 @ 添加资料」），**不能靠 placeholder 定位**。
# 另外页面上可能有多个 textarea/可编辑区，必须挑「可见且面积最大」的那个。
INPUT_GENERIC = [
    "textarea",
    "[contenteditable='true']",
    "[role='textbox']",
]


async def find_input(page):
    """挑出真正的聊天输入框：可见、宽高足够、位置偏页面下方。"""
    best, best_area = None, 0
    for css in INPUT_GENERIC:
        try:
            loc = page.locator(css)
            n = await loc.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                box = await el.bounding_box()
                if not box:
                    continue
                area = box["width"] * box["height"]
                # 输入框必须够宽；排除那些细小的隐藏 textarea
                if box["width"] < 150 or area <= best_area:
                    continue
                best, best_area = el, area
            except Exception:  # noqa: BLE001
                continue
    if best is None:
        raise RuntimeError(
            "找不到输入框 —— 页面可能未登录或已改版。跑 login.bat 重新校准。"
        )
    return best


async def fill_input(page, text: str) -> None:
    """把文本放进输入框。contenteditable 用 insert_text，textarea 用 fill。"""
    box = await find_input(page)
    tag = await box.evaluate("el => el.tagName.toLowerCase()")
    await box.click(timeout=10000)
    if tag in ("textarea", "input"):
        await box.fill(text, timeout=10000)
        return
    # contenteditable：先全选清空
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await page.wait_for_timeout(120)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i:
            await page.keyboard.press("Shift+Enter")
        if line:
            await page.keyboard.insert_text(line)


async def submit(page) -> None:
    """优先点发送按钮，退化为回车。"""
    btn = await first_visible(page, SEND_CANDIDATES, timeout_each=0.5)
    if btn is not None:
        try:
            await btn.click(timeout=8000)
            return
        except Exception:  # noqa: BLE001
            log.debug("点发送按钮失败，改用回车", exc_info=True)
    box = await find_input(page)
    await box.press("Enter")


# ── 模型选择器 ────────────────────────────────────────────
# 实测坑：工作模式下这个 testid 会匹配到**多个**按钮 ——
#   「豆包 2.1 Lite」按钮 和 「高」（推理强度）按钮 都是 chat_input_action_model。
# 必须挑出**文案里含模型名**的那个，否则会点开推理强度菜单。
MODEL_NAME_MARKERS = ("2.1", "快速", "自动", "豆包", "Doubao")


async def model_buttons(page) -> List:
    """返回所有 chat_input_action_model 按钮 locator（按 DOM 顺序）。"""
    for css in MODEL_BUTTON_CANDIDATES:
        try:
            loc = page.locator(css)
            if await loc.count():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return []


async def model_button(page):
    """挑出真正的「模型」按钮（跳过推理强度那个同 testid 的按钮）。"""
    locs = await model_buttons(page)
    if not locs:
        return None
    total = await locs.count()
    fallback = None
    for i in range(total):
        el = locs.nth(i)
        try:
            if not await el.is_visible():
                continue
            text = (await el.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if fallback is None:
            fallback = el
        if any(m in text for m in MODEL_NAME_MARKERS):
            return el
    return fallback


async def reasoning_button(page):
    """挑出「推理强度」按钮（同 testid，文案是 低/中/高）。"""
    locs = await model_buttons(page)
    if not locs:
        return None
    total = await locs.count()
    for i in range(total):
        el = locs.nth(i)
        try:
            if not await el.is_visible():
                continue
            text = (await el.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if text in ("低", "中", "高", "Low", "Medium", "High") or "推理" in text:
            return el
    return None


async def open_model_menu(page, *, retries: int = 3):
    """点开模型选择器，并等下拉项出现。返回菜单项 locator。"""
    last_err = None
    for attempt in range(retries):
        btn = await model_button(page)
        if btn is None:
            last_err = "找不到模型选择器按钮"
            await page.wait_for_timeout(600)
            continue
        try:
            await btn.click()
        except Exception as exc:  # noqa: BLE001
            last_err = f"点击模型按钮失败: {exc}"
            await page.wait_for_timeout(600)
            continue

        for css in MODEL_OPTION_CANDIDATES:
            try:
                loc = page.locator(css)
                await loc.first.wait_for(state="visible", timeout=2500)
                # 等待菜单渲染稳定，避免读到上一次的残留项
                await page.wait_for_timeout(350)
                if await loc.count() >= 1:
                    return loc
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                continue
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

    raise RuntimeError(f"打不开模型菜单：{last_err}")


async def current_model_text(page) -> str:
    """读回模型按钮上的文案。"""
    btn = await model_button(page)
    if btn is None:
        return ""
    try:
        return (await btn.inner_text()) or ""
    except Exception:  # noqa: BLE001
        return ""


async def list_model_options(page) -> List[str]:
    """打开下拉，列出所有可见选项文案，然后关掉。用于校准。"""
    out: List[str] = []
    try:
        items = await open_model_menu(page)
        if items is not None:
            n = await items.count()
            for i in range(n):
                try:
                    el = items.nth(i)
                    if await el.is_visible():
                        t = (await el.inner_text() or "").strip()
                        if t:
                            out.append(re.sub(r"\s+", " ", t))
                except Exception:  # noqa: BLE001
                    continue
    finally:
        try:
            await page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
    # 去重保序
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


async def _read_menu_labels(items) -> List[str]:
    """读出菜单项文案。菜单中途关掉也不抛异常。"""
    labels: List[str] = []
    try:
        total = await items.count()
    except Exception:  # noqa: BLE001
        return labels
    for i in range(total):
        try:
            el = items.nth(i)
            if not await el.is_visible():
                continue
            text = re.sub(r"\s+", " ", (await el.inner_text() or "")).strip()
            if text:
                labels.append(text)
        except Exception:  # noqa: BLE001
            continue
    return labels


def _looks_like_work_menu(labels: List[str]) -> bool:
    """工作模式的下拉里一定会有 2.1 / Lite / Pro / 推理强度 之一。"""
    joined = " ".join(labels)
    return any(k in joined for k in ("2.1", "Lite", "Pro", "推理强度"))


async def select_model(page, spec: ModelSpec, *, confirm_timeout: float = 6.0) -> bool:
    """在下拉菜单项里点选目标模型，然后轮询等待按钮文案确认生效。

    实测绕开的三个坑：
      1. 匹配必须限定在菜单项内部 —— 全页 get_by_text 会点到按钮本身，等于关菜单
      2. 点完之后按钮文案不会立刻变，必须轮询等，固定 sleep 会误判
      3. 工作模式下同一个 testid 有多个按钮（模型 + 推理强度），
         且切换后菜单可能残留上一次的项 —— 需要校验菜单「像不像工作模式的」
    """
    before = await current_model_text(page)

    for attempt in range(3):
        try:
            items = await open_model_menu(page)
        except RuntimeError as exc:
            log.warning("打开模型菜单失败（第 %d 次）：%s", attempt + 1, exc)
            await page.wait_for_timeout(800)
            continue

        labels = await _read_menu_labels(items)

        # 菜单残留校验：不像工作模式的菜单就关掉重来
        if labels and not _looks_like_work_menu(labels):
            log.warning("下拉内容不像工作模式菜单（%s），重试", labels)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(700)
            continue

        target = None
        for i in range(len(labels)):
            nt = normalize(labels[i])
            if any(normalize(c) in nt for c in spec.label_candidates()):
                try:
                    target = items.nth(i)
                except Exception:  # noqa: BLE001
                    target = None
                break

        if target is None:
            await page.keyboard.press("Escape")
            log.warning(
                "模型下拉里没有 %s（候选=%s）；下拉实际选项=%s",
                spec.id,
                spec.label_candidates(),
                labels,
            )
            # 「自动」这类目标确实可能不在，直接返回，不必重试
            return False

        try:
            await target.click()
        except Exception as exc:  # noqa: BLE001
            log.warning("点击菜单项失败（第 %d 次）：%s", attempt + 1, exc)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(600)
            continue

        # 轮询等待按钮文案变化
        want = normalize(spec.ui_label)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + confirm_timeout
        now = before
        while loop.time() < deadline:
            now = await current_model_text(page)
            if want and want in normalize(now) and normalize(now) != normalize(before):
                log.info("模型切换 %s：%r -> %r  OK", spec.id, before, now)
                return True
            await asyncio.sleep(0.25)

        await page.keyboard.press("Escape")
        await asyncio.sleep(0.4)
        now = await current_model_text(page)
        ok = bool(want) and want in normalize(now)
        log.info(
            "模型切换 %s：%r -> %r -> %s（目标文案 %r）",
            spec.id,
            before,
            now,
            "OK" if ok else "未确认",
            spec.ui_label,
        )
        if ok:
            return True
        # 没确认成功，再试一轮
        await page.wait_for_timeout(500)

    log.warning("模型切换 %s 连续失败，保持当前档位", spec.id)
    return False


async def set_reasoning(page, level: str) -> bool:
    """设置推理强度 low/medium/high（尽力而为，失败不致命）。"""
    from .registry import REASONING_LABELS

    labels = REASONING_LABELS.get(level)
    if not labels:
        return False
    btn = await reasoning_button(page)
    if btn is None:
        log.debug("找不到推理强度按钮")
        return False
    try:
        await btn.click()
        await page.wait_for_timeout(500)
        # 在弹出层里找目标档位
        for lab in labels:
            try:
                loc = page.get_by_text(lab, exact=True).last
                await loc.wait_for(state="visible", timeout=1500)
                await loc.click()
                await page.wait_for_timeout(400)
                log.info("推理强度已设为 %s", level)
                return True
            except Exception:  # noqa: BLE001
                continue
        await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        log.debug("设置推理强度失败", exc_info=True)
    return False


# ── 回复读取 ──────────────────────────────────────────────
_COPY_NOISE = re.compile(
    r"^(复制|重新生成|重新回答|点赞|点踩|分享|朗读|编辑|删除|引用|更多|已复制)+$"
)


async def _message_items(page):
    """返回当前页面上所有消息项 locator（按 DOM 顺序）。"""
    for css in MESSAGE_ITEM_CANDIDATES:
        try:
            loc = page.locator(css)
            n = await loc.count()
            if n:
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


async def latest_reply_text(page) -> str:
    """取最后一条助手回复的纯文本。

    策略：从最后一个消息项开始往前找，取第一个「看起来像回复」的内容块。
    同时剔除复制/点赞这类操作条文案，以及折叠的思维链。
    """
    items = await _message_items(page)
    if items is None:
        return ""

    total = await items.count()
    if total == 0:
        return ""

    # 从最后往前找最多 4 个，避免把用户消息当成回复
    for idx in range(total - 1, max(total - 5, -1), -1):
        item = items.nth(idx)
        try:
            if not await item.is_visible():
                continue
        except Exception:  # noqa: BLE001
            continue

        for css in REPLY_CONTENT_CANDIDATES:
            try:
                content = item.locator(css).last
                if await content.count() == 0:
                    continue
                text = await content.evaluate(
                    """el => {
                        const clone = el.cloneNode(true);
                        // 去掉思维链/操作条等非正文
                        clone.querySelectorAll(
                            "[class*='think'],[class*='reasoning'],[class*='chain']," +
                            "[class*='toolbar'],[class*='action'],[class*='operation']," +
                            "button,script,style"
                        ).forEach(n => n.remove());
                        return clone.innerText || '';
                    }"""
                )
                text = _clean(text)
                if text:
                    return text
            except Exception:  # noqa: BLE001
                continue

        # 兜底：整个消息项
        try:
            text = _clean(await item.inner_text())
            if text:
                return text
        except Exception:  # noqa: BLE001
            continue

    return ""


def _clean(text: str) -> str:
    if not text:
        return ""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if _COPY_NOISE.match(line):
            continue
        lines.append(line)
    out = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", out)


async def is_busy(page) -> bool:
    for marker in BUSY_MARKERS:
        try:
            if await page.locator(marker).first.is_visible(timeout=200):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ── 人机验证 ──────────────────────────────────────────────
# 实测（2026-09-24）真实形态：
#   <div id="captcha_container">                     铺满 1440x900
#   <iframe src="https://rmc.bytedance.com/verifycenter/captcha/v2?...">   380x566 居中
# 所以关键是认 verifycenter / captcha_container / rmc.bytedance.com。
# 一旦命中，上游不会返回任何正文，必须人工点掉滑块。
CAPTCHA_SELECTORS = (
    "#captcha_container",
    "iframe[src*='verifycenter']",
    "iframe[src*='rmc.bytedance.com']",
    "iframe[src*='captcha']",
    "iframe[src*='secsdk']",
    "[class*='captcha-container']",
    "[class*='captcha_container']",
)

CAPTCHA_TEXTS = (
    "请完成验证",
    "人机验证",
    "安全验证",
    "滑动验证",
    "拖动滑块",
    "请拖动",
    "验证码",
    "点击进行验证",
    "请完成安全验证",
)


async def detect_captcha(page) -> str:
    """返回命中的验证说明；没有则返回空串。

    只在**可见**时才算命中 —— captcha_container 平时就在 DOM 里，
    靠容器存在会误报，必须看尺寸。
    """
    for css in CAPTCHA_SELECTORS:
        try:
            loc = page.locator(css).first
            if await loc.count() == 0:
                continue
            box = await loc.bounding_box()
            if box and box["width"] >= 60 and box["height"] >= 40 and await loc.is_visible():
                return f"检测到验证组件 {css}（{int(box['width'])}x{int(box['height'])}）"
        except Exception:  # noqa: BLE001
            continue

    # 兜底：看有没有可见的 verifycenter iframe（有些版本没有 id）
    try:
        hit = await page.evaluate(
            """() => {
                const fs = Array.from(document.querySelectorAll('iframe'));
                for (const f of fs) {
                    const s = f.getAttribute('src') || '';
                    if (!/verifycenter|captcha|rmc\\.bytedance/i.test(s)) continue;
                    const r = f.getBoundingClientRect();
                    if (r.width >= 60 && r.height >= 40) return s.slice(0, 90);
                }
                return '';
            }"""
        )
        if hit:
            return f"检测到验证 iframe {hit}"
    except Exception:  # noqa: BLE001
        pass

    try:
        body = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:  # noqa: BLE001
        return ""
    for t in CAPTCHA_TEXTS:
        if t in (body or ""):
            return f"页面出现「{t}」"
    return ""


async def is_captcha_visible(page) -> bool:
    return bool(await detect_captcha(page))


async def raise_browser_window(page) -> bool:
    """把人机验证弹窗所在的浏览器窗口拉到前台并给出系统通知。

    目的：验证必须人工点，但用户可能在别的窗口干活看不到。
    做两件事：
      1. 通过 CDP 的 Browser.setWindowBounds 把窗口提到最前并聚焦
      2. Windows 上弹一个托盘通知（toast），点不点无所谓，能看见就行
    失败不影响主流程。
    """
    ok = False
    # 1) 用 CDP 把窗口提到前台
    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            info = await cdp.send("Browser.getWindowForTarget")
            window_id = info.get("windowId")
            if window_id is not None:
                await cdp.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {"windowState": "normal"},
                    },
                )
                # CDP 没有直接的 "focus"，但把窗口设成 normal 并激活页面通常够用
                await page.bring_to_front()
                ok = True
        finally:
            try:
                await cdp.detach()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        log.debug("CDP 提窗失败，退回 page.bring_to_front()", exc_info=True)
        try:
            await page.bring_to_front()
            ok = True
        except Exception:  # noqa: BLE001
            pass

    # 2) 系统通知（Windows 托盘气泡 / toast）
    try:
        _notify_windows(
            "豆包需要人机验证",
            "请切到浏览器窗口完成图片验证，完成后请求会自动继续。",
        )
    except Exception:  # noqa: BLE001
        log.debug("系统通知发送失败", exc_info=True)

    return ok


def notify_user(title: str, message: str) -> None:
    """给用户发系统通知。

    Windows 用托盘气泡（PowerShell NotifyIcon，不引入额外依赖）；
    其它平台退化成日志。
    这是「人机验证需要人工介入」时的提醒通道 —— 用户可能不在看终端，
    光靠日志会错过。

    ⚠️ 这是**同步**函数，调用方不要 await（曾经写成 await，
    抛 TypeError 把请求打成 502，通知也没发出去）。
    """
    if not sys.platform.startswith("win"):
        log.warning("【%s】%s", title, message)
        return
    _notify_windows(title, message)


async def show_verify(page, verify_data: str, *, aid: int = 497858) -> bool:
    """用风控返回的 decision 主动把验证界面拉到页面上。

    页面里已经加载了字节的验证 SDK，全局对象实测暴露：
        verifyCenter / verifySDK / renderSecondVerifyWeb / renderCaptcha /
        initVerifyOptions / initVerifyCenter / closeCaptcha / autoRender

    其中 `renderSecondVerifyWeb({commonOptions, verify_data})` 是「二次验证」入口，
    verify_data 就是 decision 字符串（实测形如
        {"code":"10000","type":"verify","subtype":"slide",
         "verify_scene":"doubao_message_web","log_id":"...","detail":"..."}）。

    这样就不必干等上游自己弹窗 —— 命中风控时我们主动把滑块摆到用户面前。

    ⚠️ 必须先用 aid 初始化，否则 SDK 报
       "The parameter aid is required and of type int"
    （实测：页面里 myOptions.options 是空的，说明应用还没初始化过验证中心）。

    返回是否成功调起。失败不抛异常（调用方已经会通知用户）。
    """
    if not verify_data:
        return False
    try:
        result = await page.evaluate(
            """async ([vd, aid]) => {
                const tries = [];
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));

                // 0) 验证 SDK 是懒加载的：页面刚打开时 window.verifySDK 还不存在
                //    （实测抓到过 'no-sdk'）。这里先等它出现，最多 10 秒。
                const findSdk = () => window.verifySDK || window.verifyCenter;
                let waited = 0;
                while (!findSdk() && waited < 10000) {
                    await sleep(300);
                    waited += 300;
                }
                // 还没出现就主动把 SDK 脚本拉起来（页面里能找到它的 URL）
                if (!findSdk()) {
                    const urls = performance.getEntriesByType('resource')
                        .map(e => e.name)
                        .filter(u => /verifycenter|secsdk-captcha/i.test(u));
                    if (urls.length) {
                        tries.push('loading-sdk');
                        await new Promise((res) => {
                            const s = document.createElement('script');
                            s.src = urls[0];
                            s.onload = res;
                            s.onerror = res;
                            document.head.appendChild(s);
                        });
                        await sleep(1500);
                    }
                }

                const sdk = findSdk() || window.verifyCenter || window.verifySDK;
                if (!sdk) return 'no-sdk|waited=' + waited;

                // 1) 初始化：缺 aid 会直接报 "The parameter aid is required and of type int"
                const common = {
                    aid: aid,
                    region: 'cn',
                    language: 'zh',
                    scene_level: 'p2',
                };
                try {
                    if (typeof window.initVerifyOptions === 'function') {
                        window.initVerifyOptions({ commonOptions: common,
                                                   captchaOptions: { mode: 'popup' } });
                        tries.push('initVerifyOptions');
                    } else if (typeof sdk.initVerifyOptions === 'function') {
                        sdk.initVerifyOptions({ commonOptions: common,
                                                captchaOptions: { mode: 'popup' } });
                        tries.push('sdk.initVerifyOptions');
                    }
                } catch (e) { tries.push('init-fail:' + String(e)); }

                // 2) 回调：成功/关闭都留痕，便于后续判断
                const hooks = {
                    successCb: () => { window.__dshVerifyOk = true; },
                    closeCb:   () => { window.__dshVerifyClosed = true; },
                };

                // 3) 调起二次验证
                try {
                    if (typeof sdk.renderSecondVerifyWeb === 'function') {
                        await sdk.renderSecondVerifyWeb(Object.assign({
                            commonOptions: common,
                            verify_data: vd,
                        }, hooks));
                        return 'ok-second|' + tries.join(',');
                    }
                    if (typeof sdk.renderCaptcha === 'function') {
                        await sdk.renderCaptcha(Object.assign({
                            captchaOptions: { mode: 'popup' },
                            commonOptions: common,
                            verify_data: vd,
                        }, hooks));
                        return 'ok-captcha|' + tries.join(',');
                    }
                } catch (e) {
                    return 'render-fail:' + String(e) + '|' + tries.join(',');
                }
                return 'no-api|' + tries.join(',');
            }""",
            [verify_data, aid],
        )
        log.info("拉起验证界面结果：%s", result)
        return str(result).startswith("ok")
    except Exception:  # noqa: BLE001
        log.warning("拉起验证界面失败", exc_info=True)
        return False


def _notify_windows(title: str, message: str) -> None:
    """Windows 托盘通知。用 PowerShell 的 NotifyIcon，不引入额外依赖。"""
    import subprocess
    import sys

    if not sys.platform.startswith("win"):
        return

    # 单引号转义，避免注入
    t = title.replace("'", "''")
    m = message.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Warning;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(15000, '{t}', '{m}', [System.Windows.Forms.ToolTipIcon]::Warning);"
        "Start-Sleep -Seconds 12;"
        "$n.Dispose();"
    )
    creationflags = 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def start_new_conversation(page) -> bool:
    """尝试开一个新会话，避免上一次的上下文串味。"""
    for css in (
        "[data-testid='new-chat']",
        "button:has-text('新工作任务')",
        "button:has-text('新对话')",
        "[class*='new-chat']",
        "[class*='create-chat']",
    ):
        try:
            loc = page.locator(css).first
            if await loc.is_visible(timeout=500):
                await loc.click()
                await page.wait_for_timeout(600)
                return True
        except Exception:  # noqa: BLE001
            continue
    # 退而求其次：直接回首页
    try:
        await page.goto(page.url.split("/chat")[0] + "/chat/", wait_until="domcontentloaded")
        await page.wait_for_timeout(800)
        return True
    except Exception:  # noqa: BLE001
        return False
