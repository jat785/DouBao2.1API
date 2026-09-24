"""模型注册表：把 OpenAI 的 model 名映射到豆包 2.1 的三个真实档位。

═══════════════════════════════════════════════════════════════
本项目的模型切换方式（与参考项目 SeiShonagon520/doubao2api 的本质区别）

那个项目把 doubao-2.1-turbo / doubao-2.1-pro 都映射成同一个值，
两个名字发出去的请求**完全相同**，只是改了返回体里的 model 字段 —— 即假档位。

本项目改成：**每个模型对应一份实测抓下来的真实请求模板**
（templates/<model>.json，由 tools.capture_templates 录制）。
差异落在请求体的这些字段上：

    模型                 model_item_key    need_deep_think
    doubao-2.1-lite      seed-lite-7b      10001
    doubao-2.1-turbo     4                 4
    doubao-2.1-pro       5                 5

三个都是实测值（2026-09-24，在「工作」页签点选后抓包得到）。
注意 need_deep_think 在**这个前端版本**上是 10001 / 4 / 5，
不是参考项目写的 0 / 1 / 3（那是另一个版本的取值）。

切换模型不再点界面，而是改请求体 —— 因为点界面在这里极其脆弱：
一旦弹人机验证，浮层会盖住整个页面（<html> intercepts pointer events），
输入框、发送按钮、模型选择器全部点不动。
═══════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"


@dataclass(frozen=True)
class ModelSpec:
    """一个对外暴露的模型。"""

    id: str                          # OpenAI 侧的 model 名
    model_item_key: str              # 上游请求体里的真实模型标识
    need_deep_think: int             # 上游档位值（实测，非 0/1/3）
    ui_label: str                    # 界面上对应文案（仅供参考/抓模板用）
    note: str = ""

    @property
    def template_file(self) -> Path:
        return TEMPLATE_DIR / f"{self.id}.json"

    @property
    def template_ready(self) -> bool:
        return self.template_file.exists()

    def label_candidates(self) -> List[str]:
        """模型下拉菜单里可能出现的文案，按优先级。

        仅 capture_templates.py（录模板时点界面）和 sel.select_model 需要。
        API 路径完全不用它 —— 切模型靠请求体里的 model_item_key。

        之所以要多候选：菜单文案会带会变的后缀，实测见过
        「豆包 2.1 Lite 0921 新版」「豆包 2.1 Turbo 专家」「豆包 2.1 Pro 升级」，
        而按钮回读只有「豆包 2.1 Lite」。
        """
        raw = [self.ui_label, self.id]
        out: List[str] = []
        for item in raw:
            item = (item or "").strip()
            if not item:
                continue
            out.append(item)
            # 去掉「豆包」/「Doubao」前缀的写法（如 "2.1 Turbo"）
            for prefix in ("豆包 ", "豆包", "Doubao ", "doubao "):
                if item.startswith(prefix):
                    out.append(item[len(prefix):].strip())
            out.append(item.replace(" ", ""))
        seen, uniq = set(), []
        for x in out:
            if x and x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq


# ── 模型表（实测值）────────────────────────────────────────
# 只暴露三个真实的 2.1 档位。
#
# 曾经给每个档位挂过 gpt-4o / deepseek-chat 之类的兼容别名（方便硬编码了
# 模型名的客户端直接接入），但那些名字容易与真正的大模型混淆，也掩盖了
# 「实际上只有三个档位」这件事，所以全部删掉。
_DEFAULT_MODELS: List[ModelSpec] = [
    ModelSpec(
        id="doubao-2.1-lite",
        model_item_key="seed-lite-7b",
        need_deep_think=10001,
        ui_label="豆包 2.1 Lite",
        note="实测：model_item_key=seed-lite-7b, need_deep_think=10001, 256k 窗口",
    ),
    ModelSpec(
        id="doubao-2.1-turbo",
        model_item_key="4",
        need_deep_think=4,
        ui_label="豆包 2.1 Turbo",
        note="实测：model_item_key=4, need_deep_think=4, 256k 窗口",
    ),
    ModelSpec(
        id="doubao-2.1-pro",
        model_item_key="5",
        need_deep_think=5,
        ui_label="豆包 2.1 Pro",
        note="实测：model_item_key=5, need_deep_think=5, 256k 窗口",
    ),
]


def _env_override(model_id: str) -> Optional[str]:
    safe = model_id.replace(".", "_").replace("-", "_")
    return os.environ.get(f"DOUBAO_MODEL_ID__{safe}", "").strip() or None


MODELS: Dict[str, ModelSpec] = {s.id: s for s in _DEFAULT_MODELS}

DEFAULT_MODEL = os.environ.get("DOUBAO_DEFAULT_MODEL", "doubao-2.1-turbo")

# 推理强度：对外接受 low/medium/high，映射到上游 reasoning_effort 取值
REASONING_EFFORT: Dict[str, str] = {
    "low": "1",
    "medium": "2",
    "high": "3",
}


def resolve(model_name: Optional[str]) -> ModelSpec:
    """把任意 model 名解析成 ModelSpec；未知名字回退到默认，不报 400。"""
    if not model_name:
        return MODELS[DEFAULT_MODEL]
    name = model_name.strip()
    if name in MODELS:
        return MODELS[name]
    lowered = name.lower()
    for spec in MODELS.values():
        if lowered.startswith(spec.id):
            return spec
    for spec in MODELS.values():
        if spec.id.split("-")[-1] in lowered and "doubao" in lowered:
            return spec
    return MODELS[DEFAULT_MODEL]


def missing_templates() -> List[str]:
    """哪些模型还没抓模板（server 启动时提示用）。"""
    return [s.id for s in MODELS.values() if not s.template_ready]


def openai_model_list() -> List[dict]:
    """给 GET /v1/models 用。只列三个真实档位，没有别名。"""
    out = []
    for spec in MODELS.values():
        out.append(
            {
                "id": spec.id,
                "object": "model",
                "created": 0,
                "owned_by": "doubao-work",
            }
        )
    return out
