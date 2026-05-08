from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tracker.recorder import record
from tracker.runner import run


def main() -> int:
    parser = argparse.ArgumentParser(prog="tracker", description="Web Tracking Analyzer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run an action script and capture network traffic")
    run_p.add_argument("script", type=Path, help="Path to action YAML script")
    run_p.add_argument("--out", type=Path, default=None, help="Output directory (default: runs/<timestamp>_<session>)")
    run_p.add_argument("--headless", action="store_true", help="Run in headless mode")

    rec_p = sub.add_parser("record", help="Record browser interactions to an action YAML script")
    rec_p.add_argument("url", help="Starting URL to open")
    rec_p.add_argument("output", type=Path, help="Output YAML path (e.g. scripts/my-flow.yaml)")
    rec_p.add_argument("--session", default=None, help="Session name (defaults to output filename stem)")
    rec_p.add_argument("--settle-ms", type=int, default=1500, metavar="MS",
                       help="Default settle delay added after each recorded action (default: 1500)")
    rec_p.add_argument("--width", type=int, default=1280, help="Viewport width (default: 1280)")
    rec_p.add_argument("--height", type=int, default=800, help="Viewport height (default: 800)")

    args = parser.parse_args()

    if args.cmd == "run":
        run(args.script, out_dir=args.out, headless=args.headless)
        return 0

    if args.cmd == "record":
        record(
            start_url=args.url,
            output_path=args.output,
            session_name=args.session,
            viewport_width=args.width,
            viewport_height=args.height,
            default_settle_ms=args.settle_ms,
        )
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
