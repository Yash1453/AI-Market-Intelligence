"""Command-line interface for the standalone market bot."""

from __future__ import annotations

import argparse
import logging
import sys

from app.data.adapters.base import MissingAPIKeyError
from market_bot.config import load_settings
from market_bot.engine import run_loop, run_market_report, run_performance_cycle, run_signal_scan


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone market intelligence and paper-analysis bot.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("report", help="Generate one hourly market snapshot.")
    subparsers.add_parser("signals", help="Scan once for validated crossover signals.")
    subparsers.add_parser("performance", help="Evaluate paper predictions once.")
    run_parser = subparsers.add_parser("run", help="Run one or all loops continuously.")
    run_parser.add_argument(
        "--loop",
        choices=("all", "report", "signals", "performance"),
        default="all",
        help="Choose one loop or all loops.",
    )
    run_parser.add_argument("--once", action="store_true", help="Run the selected loop once and exit.")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings()
        if args.command == "report":
            print(run_market_report(settings))
        elif args.command == "signals":
            print(f"New signals: {run_signal_scan(settings)}")
        elif args.command == "performance":
            print(run_performance_cycle(settings))
        else:
            run_loop(settings, args.loop, once=args.once)
    except KeyboardInterrupt:
        print("Market bot stopped.")
    except MissingAPIKeyError as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        logging.getLogger(__name__).error("Market bot run failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
