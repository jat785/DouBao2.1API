"""集中配置：全部来自环境变量，无硬编码凭据。

支持项目根目录下的 `.env`（可选）。加载规则：
  - 只读**当前进程**的环境，不写注册表、不改用户环境变量
  - **已有的系统环境变量优先**（用 setdefault），所以临时覆盖依然可以：
        set DOUBAO_PORT=8899 && start.bat

注意：豆包是国内服务，默认**不走系统代理**。若你的网络环境必须走代理访问
www.doubao.com，再显式设置 DOUBAO_PROXY。
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """把项目根目录的 .env 读进 os.environ（不覆盖已存在的变量）。

    为什么手写而不用 python-dotenv：只需要读 KEY=VALUE 这一种格式，
    十几行就够，不值得为此增加一个依赖。

    ⚠️ 必须在下面任何 os.environ.get() 之前调用，否则读到的还是旧值。
    """
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # 去掉成对的引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # ⚠️ 空值不要写进环境变量。
        #
        # `.env.example` 里大量变量故意留空表示「用默认值」（例如
        # DOUBAO_USER_DATA_DIR=、DOUBAO_PROXY=）。若在这里设成空字符串，
        # 后面的 os.environ.get(key, 默认) 会拿到 '' 而不是默认值 ——
        # 实测踩过：DOUBAO_USER_DATA_DIR= 会让 profile 变成当前目录 "."。
        if not value:
            continue
        os.environ.setdefault(key, value)


_load_dotenv()

# ── 服务监听 ──────────────────────────────────────────────
HOST = os.environ.get("DOUBAO_HOST", "127.0.0.1")
PORT = int(os.environ.get("DOUBAO_PORT", "8787"))

# OpenAI 兼容层鉴权：留空则不做任何校验（仅建议本机自用）
API_KEY = os.environ.get("DOUBAO_API_KEY", "")

# ── 上游网页 ──────────────────────────────────────────────
# 豆包主站聊天页。桌面版「豆包工作」渲染的就是这套前端。
CHAT_URL = os.environ.get("DOUBAO_CHAT_URL", "https://www.doubao.com/chat/")

# 只用于「豆包工作」桌面端/其它前端变体时覆盖
SITE_SELECTOR_HINT = os.environ.get("DOUBAO_SITE_HINT", "")

# ── 浏览器 / 登录态 ───────────────────────────────────────
# 持久化目录：登录一次，长期复用
USER_DATA_DIR = Path(
    os.environ.get("DOUBAO_USER_DATA_DIR", str(Path.home() / ".DouBao2.1API" / "browser"))
).expanduser()

# 当前账号的显示名。
#
# ⚠️ 多账号「只预留接口，不实现切换」：
#    浏览器会话绑定到「当前账号」的 user-data-dir，账号切换 = 换掉
#    USER_DATA_DIR 再重启。同一个时刻只跑一个 Chrome（一个号用完再切下一个），
#    所以不需要多实例。
ACCOUNT_LABEL = os.environ.get("DOUBAO_ACCOUNT_LABEL", "default")

HEADLESS = os.environ.get("DOUBAO_HEADLESS", "false").lower() in ("1", "true", "yes")

# 浏览器可执行文件（留空用 Playwright 自带 chromium）
BROWSER_EXECUTABLE = os.environ.get("DOUBAO_BROWSER_EXECUTABLE", "")

# 本机代理；默认空 = 直连
PROXY = os.environ.get("DOUBAO_PROXY", "")

# Chrome 远程调试端口。
# 设成非 0 时：启动的浏览器会带 --remote-debugging-port，
# 后续进程（调试脚本 / 第二个实例）会**优先附加到这个已有浏览器**，
# 而不是自己去抢 user-data-dir（同一个 profile 不能被两个进程同时打开）。
CDP_PORT = int(os.environ.get("DOUBAO_CDP_PORT", "9333"))

# ── 调度与节流 ────────────────────────────────────────────
# 两次上游请求之间的最小间隔（秒）。
#
# 实测：连打 12 次会拿到 710022004「rate limited」（extra.decision.type=verify），
# 所以默认给得比较保守。网页版本来就是串行 agent loop，QPS 上限 ≈ 1 并不吃亏。
MIN_INTERVAL = float(os.environ.get("DOUBAO_MIN_INTERVAL", "3.0"))

# 命中限流后的冷却时长（秒）：期间请求立即返回错误，不再打上游，
# 避免把临时限流拖成长期封禁。
RATE_LIMIT_COOLDOWN = float(os.environ.get("DOUBAO_RATE_LIMIT_COOLDOWN", "60"))

# 人机验证系统通知的冷却（秒）：连续失败时不要反复弹窗打扰用户
CAPTCHA_NOTIFY_COOLDOWN = float(
    os.environ.get("DOUBAO_CAPTCHA_NOTIFY_COOLDOWN", "300")
)

# ── 额度查看 ──────────────────────────────────────────────
# 额度页官方地址（实测：账号菜单里的「额度状态」就跳这里）
QUOTA_PAGE_URL = os.environ.get(
    "DOUBAO_QUOTA_URL",
    "https://www.doubao.com/member/quota-management"
    "?enter_method=avatar_menu&quota_tab=personal",
)

# 抓一次额度要新开标签、导航、等接口返回，实测约 7 秒 —— 必须缓存。
QUOTA_CACHE_TTL = float(os.environ.get("DOUBAO_QUOTA_CACHE_TTL", "60"))

# 抓取超时（秒）
QUOTA_TIMEOUT = float(os.environ.get("DOUBAO_QUOTA_TIMEOUT", "45"))

# 单次请求总超时（秒）。网页版是 agent loop，慢请求很常见
REQUEST_TIMEOUT = float(os.environ.get("DOUBAO_REQUEST_TIMEOUT", "180"))

# 首字节超时（秒）：等模型开始输出
FIRST_TOKEN_TIMEOUT = float(os.environ.get("DOUBAO_FIRST_TOKEN_TIMEOUT", "120"))

# 等待回复「稳定」的判定：连续 N 次轮询文本未变则视为完成（DOM 兜底模式用）
DOM_STABLE_ROUNDS = int(os.environ.get("DOUBAO_DOM_STABLE_ROUNDS", "4"))
DOM_POLL_INTERVAL = float(os.environ.get("DOUBAO_DOM_POLL_INTERVAL", "0.5"))

# 命中人机验证后，额外等待人工完成的时长（秒）
CAPTCHA_MANUAL_WAIT = float(os.environ.get("DOUBAO_CAPTCHA_MANUAL_WAIT", "180"))

# 每个请求是否新建会话（避免上下文串味）；false 则在同一会话内追加
NEW_CONVERSATION_PER_REQUEST = os.environ.get(
    "DOUBAO_NEW_CONVERSATION", "true"
).lower() in ("1", "true", "yes")

# ── 日志 ─────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("DOUBAO_LOG_LEVEL", "INFO")
LOG_DIR = Path(os.environ.get("DOUBAO_LOG_DIR", str(Path.cwd() / "logs"))).expanduser()
