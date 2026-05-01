from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from signal_bot.bot import SignalBotConfig, SignalDrivenOptionBot
from signal_bot.models import EntryMode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Signal-driven Schwab options bot")
    parser.add_argument("--signal-file", type=Path, help="Path to raw signal text file")
    parser.add_argument("--quantity", type=int, default=4, help="Contracts to trade; must be a multiple of 4")
    parser.add_argument("--mode", choices=["aggro", "moderate"], help="Optional preselected mode")
    parser.add_argument("--market-timezone", default="America/New_York", help="Timezone for cutoff/expiry handling")
    parser.add_argument("--poll-seconds", type=float, default=1.0, help="Quote polling interval")
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    raw_signal = args.signal_file.read_text() if args.signal_file else sys.stdin.read()
    if not raw_signal.strip():
        print("No signal text provided.", file=sys.stderr)
        return 1

    config = SignalBotConfig(
        market_timezone=args.market_timezone,
        poll_interval_seconds=args.poll_seconds,
        default_quantity=args.quantity,
    )
    bot = SignalDrivenOptionBot(config=config)
    mode = EntryMode(args.mode) if args.mode else None
    runtime = await bot.run_signal(raw_signal=raw_signal, quantity=args.quantity, mode=mode)
    print(f"Final status: {runtime.status.value}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
