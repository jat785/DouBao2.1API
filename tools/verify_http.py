"""验证 HTTP 层的流式输出：确认 chunk 是真增量、并且以 [DONE] 收尾。

同时测非流式，并打印每个 delta 的时间戳，用来判断「是不是真流式」。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8791"


def post(path: str, body: dict):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    return urllib.request.urlopen(req, timeout=300)


def test_stream(model: str) -> bool:
    print(f"\n{'='*58}\n流式测试 {model}\n{'='*58}", flush=True)
    q = "用一句话说明什么是递归"
    t0 = time.monotonic()
    deltas = []
    done = False
    meta = None
    try:
        resp = post("/v1/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": q}],
            "stream": True,
        })
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                done = True
                break
            try:
                obj = json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
            if obj.get("doubao_meta"):
                meta = obj["doubao_meta"]
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    deltas.append((round(time.monotonic() - t0, 2), d["content"]))
    except Exception as exc:  # noqa: BLE001
        print(f"  异常: {type(exc).__name__}: {exc}", flush=True)
        return False

    text = "".join(d for _, d in deltas)
    print(f"  收到 {len(deltas)} 个增量, 共 {len(text)} 字, 用时 {time.monotonic()-t0:.1f}s", flush=True)
    if deltas:
        print("  前 6 个增量时间点:", [f"{t}s" for t, _ in deltas[:6]], flush=True)
        spread = deltas[-1][0] - deltas[0][0]
        print(f"  首末增量跨度: {spread:.1f}s  ->", "真流式" if spread > 0.05 else "一次性到达（伪流式）", flush=True)
    print(f"  正文: {text[:120]!r}", flush=True)
    print(f"  [DONE] 收到: {done}", flush=True)
    print(f"  meta: {meta}", flush=True)
    ok = done and bool(text.strip())
    print("  结果:", "通过" if ok else "失败", flush=True)
    return ok


def test_nonstream(model: str) -> bool:
    print(f"\n{'-'*58}\n非流式 {model}", flush=True)
    t0 = time.monotonic()
    try:
        resp = post("/v1/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": "只回答两个字：可以"}],
        })
        obj = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"  HTTP {exc.code}: {exc.read().decode('utf-8','replace')[:300]}", flush=True)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  异常: {type(exc).__name__}: {exc}", flush=True)
        return False
    text = obj["choices"][0]["message"]["content"]
    print(f"  {time.monotonic()-t0:.1f}s 回答={text!r}", flush=True)
    print(f"  item_key={obj['doubao_meta'].get('model_item_key')} usage={obj['usage']}", flush=True)
    return bool(text.strip())


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "doubao-2.1-lite"
    results = {}
    results["stream"] = test_stream(target)
    results["nonstream"] = test_nonstream(target)
    print(f"\n{'='*58}")
    for k, v in results.items():
        print(f"  {'[OK]' if v else '[FAIL]'} {k}")
    sys.exit(0 if all(results.values()) else 1)
