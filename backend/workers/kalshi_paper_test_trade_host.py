"""Minimal process host for explicit Kalshi paper-only test-trade runs."""

from __future__ import annotations

import asyncio
import os

os.environ["HOMERUN_PROCESS_ROLE"] = "worker"
os.environ["HOMERUN_WORKER_PLANE"] = "kalshi_paper_test"

from workers.kalshi_paper_test_trade_worker import start_loop


def main() -> None:
    asyncio.run(start_loop())


if __name__ == "__main__":
    main()
