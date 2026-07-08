from __future__ import annotations

import argparse
import sys
from typing import Literal

from radar.config import Settings, get_settings
from radar.db import get_connection, init_db, list_pending_alerts, resolve_alert

Decision = Literal["approved", "rejected"]


def list_pending(settings: Settings | None = None) -> list[tuple]:
    settings = settings or get_settings()
    conn = get_connection(settings.database_path)
    init_db(conn)
    try:
        return list_pending_alerts(conn)
    finally:
        conn.close()


def review(post_id: str, decision: Decision, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    conn = get_connection(settings.database_path)
    init_db(conn)
    try:
        return resolve_alert(conn, post_id, decision)
    finally:
        conn.close()


def _print_pending(settings: Settings | None = None) -> None:
    pending = list_pending(settings)
    if not pending:
        print("No pending alerts.")
        return
    for post_id, category, severity, velocity, issue_summary, url in pending:
        print(f"{post_id}\t{category}\t{severity}\tvelocity={velocity:.1f}\t{issue_summary}\t{url}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="radar review")
    sub = parser.add_subparsers(dest="action", required=False)
    sub.add_parser("list", help="List alerts pending human QA review.")
    approve = sub.add_parser("approve", help="Approve a pending alert.")
    approve.add_argument("post_id")
    reject = sub.add_parser("reject", help="Reject a pending alert.")
    reject.add_argument("post_id")

    args = parser.parse_args(argv)
    action = args.action or "list"

    if action == "list":
        _print_pending()
    elif action in ("approve", "reject"):
        decision: Decision = "approved" if action == "approve" else "rejected"
        changed = review(args.post_id, decision)
        if changed:
            print(f"{decision.capitalize()} {args.post_id}.")
        else:
            print(f"No pending alert found for {args.post_id}.")
            sys.exit(1)


if __name__ == "__main__":
    main()
