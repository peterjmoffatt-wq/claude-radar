from __future__ import annotations

import argparse

from radar import collect


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="radar")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="Run one collection pass and write snapshots to SQLite.")
    # `radar serve` (FastAPI dashboard) lands in a later phase.

    args = parser.parse_args(argv)

    if args.command == "collect":
        collect.main()


if __name__ == "__main__":
    main()
