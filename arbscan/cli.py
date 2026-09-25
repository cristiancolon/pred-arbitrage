"""Command-line entry point: ``arbscan <command>``."""

import argparse
import asyncio
import logging
import sys

from . import catalog, config, jev, match, report, review, scanner, store


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="arbscan", description="Read-only Kalshi <-> Polymarket US arbitrage scanner.")
    ap.add_argument("-c", "--config", help="TOML config file (default: ./config.toml if present)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("catalog", help="download both venues' open markets for matching")
    sub.add_parser("match", help="suggest Kalshi <-> Polymarket US pairs from the catalog")
    r = sub.add_parser("review", help="approve or reject suggested pairs")
    r.add_argument("--min-score", type=float, default=None)
    r.add_argument("--list", action="store_true", help="print pending candidates instead of prompting")
    r.add_argument("--limit", type=int, default=100)
    a = sub.add_parser("autoreview", help="let Jev (TypeSafe) approve or reject pending candidates")
    a.add_argument("--limit", type=int, default=None, help="review at most this many (default from config)")
    a.add_argument("--dry-run", action="store_true", help="print verdicts without recording anything")
    s = sub.add_parser("scan", help="watch approved pairs and record opportunities")
    s.add_argument("--once", action="store_true", help="run a single sweep and exit")
    sv = sub.add_parser("serve", help="scanner + scheduled refresh + web dashboard")
    sv.add_argument("--host", help="dashboard bind address (default from config: 0.0.0.0)")
    sv.add_argument("--port", type=int, help="dashboard port (default from config: 8787)")
    sv.add_argument("--no-scanner", action="store_true", help="dashboard and refresh job only")
    rp = sub.add_parser("report", help="summarize what the scanner found")
    rp.add_argument("--hours", type=float, default=24.0)
    rp.add_argument("--min-profit", type=float, default=0.0, help="ignore windows below this $ profit")
    rp.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = config.load(args.config)
    db = store.connect(cfg.db_path)
    try:
        if args.cmd == "catalog":
            asyncio.run(catalog.build(cfg, db))
        elif args.cmd == "match":
            match.run(cfg, db)
        elif args.cmd == "review":
            min_score = cfg.match_min_score if args.min_score is None else args.min_score
            if args.list:
                review.list_candidates(db, min_score, args.limit, cfg.pairs_path)
            else:
                review.run(cfg, db, min_score)
        elif args.cmd == "autoreview":
            asyncio.run(jev.review(cfg, db, limit=args.limit, dry_run=args.dry_run))
        elif args.cmd == "scan":
            asyncio.run(scanner.run(cfg, db, once=args.once))
        elif args.cmd == "serve":
            from dataclasses import replace

            from .service import serve  # imports the web stack only when needed

            cfg = replace(cfg, web_host=args.host or cfg.web_host, web_port=args.port or cfg.web_port)
            asyncio.run(serve(cfg, args.config, db, run_scanner=not args.no_scanner))
        elif args.cmd == "report":
            report.run(db, args.hours, args.min_profit, args.top)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()


if __name__ == "__main__":
    main()
