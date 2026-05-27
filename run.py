#!/usr/bin/env python3
import argparse
import logging
import random
import sys
from pathlib import Path

import pandas as pd

import scraper
import verifier


def _parse_args():
    """Set up and process CLI args."""
    parser = argparse.ArgumentParser(
        description="Scrape a trade group member directory and produce a structured CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run.py --source land-trust-alliance --out members.csv\n"
            "  python run.py --source land-trust-alliance --verify-sample 15"
        ),
    )
    parser.add_argument(
        "--source",
        required=True,
        metavar="KEY",
        help="Source key defined in sources.yaml (e.g. land-trust-alliance)",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="Output CSV path (default: output/<source>.csv)",
    )
    parser.add_argument(
        "--verify-sample",
        type=int,
        default=0,
        metavar="N",
        help="Randomly verify N orgs against ProPublica Nonprofit Explorer (default: 0 = skip)",
    )
    return parser.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger(__name__)

    args = _parse_args()

    # Use user explicit destination or fallback to output/<source_name>.csv & create parent dir if needed
    out_path = Path(args.out) if args.out else Path("output") / f"{args.source}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("Source  : %s", args.source)
    log.info("Output  : %s", out_path)

    # Collect & scrape data rows
    records: list[dict] = []
    try:
        for record in scraper.scrape(args.source):
            records.append(record)
            if len(records) % 50 == 0:
                log.info("  … %d records collected", len(records))
    except KeyError as exc:
        log.error("Unknown source key %s — check sources.yaml", exc)
        sys.exit(1)

    log.info("Scraped %d records", len(records))

    # Verify if needed
    if args.verify_sample and records:
        n = min(args.verify_sample, len(records))
        sample = random.sample(records, n)
        log.info("Verifying %d orgs against ProPublica …", n)
        verifier.verify_sample(sample)
        verified_count = sum(1 for r in sample if r.get("verified_nonprofit") == "true")
        log.info("Verification done  (%d/%d matched)", verified_count, n)

    # Convert records to a df & output CSV at defined path
    df = pd.DataFrame(records)
    df.to_csv(out_path, index=False)
    log.info("Wrote %d rows → %s", len(df), out_path)


if __name__ == "__main__":
    main()
