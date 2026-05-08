from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tracker.runner import run


def main() -> int:
    parser = argparse.ArgumentParser(prog="tracker", description="Web Tracking Analyzer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run an action script and capture network traffic")
    run_p.add_argument("script", type=Path, help="Path to action YAML script")
    run_p.add_argument("--out", type=Path, default=None, help="Output directory (default: runs/<timestamp>_<session>)")
    run_p.add_argument("--headless", action="store_true", help="Run in headless mode")

    args = parser.parse_args()

    if args.cmd == "run":
        run(args.script, out_dir=args.out, headless=args.headless)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
