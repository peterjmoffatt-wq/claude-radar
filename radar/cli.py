from __future__ import annotations

import argparse

from radar import api, backtest, classify, cluster, collect, leadtime, qa, score


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="radar")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="Run one collection pass and write snapshots to SQLite.")
    sub.add_parser(
        "classify", help="Classify unclassified snapshots via Claude and write results to SQLite."
    )
    sub.add_parser(
        "score", help="Score classified pain points by engagement velocity and write alerts."
    )
    review_parser = sub.add_parser(
        "review", help="List/approve/reject alerts pending human QA (abuse/credential_theft/safety)."
    )
    review_parser.add_argument("review_args", nargs=argparse.REMAINDER)
    sub.add_parser("clusters", help="Print root-cause clusters of scored alerts.")
    sub.add_parser("leadtime", help="Print the lead-time summary (early-warning vs. top pass).")
    sub.add_parser(
        "backtest", help="Backtest scored alerts against config/known_incidents.yaml."
    )
    sub.add_parser("serve", help="Serve the FastAPI + static dashboard on http://127.0.0.1:8000.")

    args = parser.parse_args(argv)

    if args.command == "collect":
        collect.main()
    elif args.command == "classify":
        classify.main()
    elif args.command == "score":
        score.main()
    elif args.command == "review":
        qa.main(args.review_args)
    elif args.command == "clusters":
        cluster.main()
    elif args.command == "leadtime":
        leadtime.main()
    elif args.command == "backtest":
        backtest.main()
    elif args.command == "serve":
        api.main()


if __name__ == "__main__":
    main()
