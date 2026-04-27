"""CLI entry point for the ingestion pipeline."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date

from sqlalchemy import func

from app.config import load_yaml
from app.db import RWSession
from app.ingestion import pipeline
from app.models import Company, Document

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)


def cmd_status() -> None:
    with RWSession() as session:
        rows = (
            session.query(Company.ticker, Document.status, func.count(Document.document_id))
            .outerjoin(Document, Company.company_id == Document.company_id)
            .group_by(Company.ticker, Document.status)
            .order_by(Company.ticker)
            .all()
        )
    if not rows:
        print("No companies or documents found. Run ingestion first.")
        return
    for ticker, status, count in rows:
        print(f"  {ticker}: {status or 'no documents'} × {count}")


async def run_ingest(tickers: list[str], since: date, summary_method: str) -> None:
    with RWSession() as session:
        pipeline.seed_companies(session)

        for ticker in tickers:
            company = session.query(Company).filter_by(ticker=ticker.upper()).first()
            if not company:
                logger.error("Ticker %s not in config.yaml — skipping", ticker)
                continue

            result = await pipeline.ingest_company(company, since, session, summary_method)

            if result.errors:
                for err in result.errors:
                    logger.error("%s error: %s", ticker, err)
            logger.info(
                "%s: done — xbrl=%d items | filings fetched=%d skipped=%d",
                ticker, result.xbrl_line_items, result.filings_fetched, result.filings_skipped,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Financial Analysis ingestion pipeline")
    parser.add_argument("--tickers", help="Comma-separated tickers, e.g. AAPL,MSFT")
    parser.add_argument("--all", action="store_true", dest="all_companies",
                        help="Ingest all companies in config.yaml")
    parser.add_argument("--since", default="2022-01-01",
                        help="Only ingest filings/facts on or after this date (YYYY-MM-DD)")
    parser.add_argument("--summary-method", choices=["extractive", "llm"], default="extractive",
                        help="Section summary strategy (Phase 3c)")
    parser.add_argument("--status", action="store_true", help="Show ingestion status and exit")
    args = parser.parse_args()

    if args.status:
        cmd_status()
        return

    cfg = load_yaml()

    if args.all_companies:
        tickers = [c["ticker"] for c in cfg.get("companies", [])]
    elif args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",")]
    else:
        parser.print_help()
        sys.exit(1)

    try:
        since = date.fromisoformat(args.since)
    except ValueError:
        logger.error("Invalid --since value: %s  (expected YYYY-MM-DD)", args.since)
        sys.exit(1)

    asyncio.run(run_ingest(tickers, since, args.summary_method))


if __name__ == "__main__":
    main()
