"""Minimal process host for the default-off Kalshi portfolio sync worker."""

from __future__ import annotations

import asyncio
import os

os.environ["HOMERUN_PROCESS_ROLE"] = "worker"
os.environ["HOMERUN_WORKER_PLANE"] = "kalshi_portfolio"

from workers.kalshi_portfolio_sync_worker import start_loop


def main() -> None:
    asyncio.run(start_loop())


if __name__ == "__main__":
    main()
