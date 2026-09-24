"""python -m BrowserViewer 启动入口。"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config


def main() -> int:
    parser = argparse.ArgumentParser(prog="BrowserViewer")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--headless", action="store_true", default=config.HEADLESS)
    parser.add_argument("--log-level", default=config.LOG_LEVEL)
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    import uvicorn

    uvicorn.run(
        "BrowserViewer.server:app",
        host=args.host,
        port=args.port,
        log_level=str(args.log_level).lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
