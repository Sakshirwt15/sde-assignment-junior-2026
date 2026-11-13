from __future__ import annotations

import argparse
from pathlib import Path

from .db import DEFAULT_DB_PATH, get_connection
from .ingest import ingest_drop
from .report import generate_reports

DEFAULT_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m solution",
        description="Interaction Ledger & Reporting Pipeline (Assignment A)",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help="Path to SQLite database file (default: ledger.db or LEDGER_DB_PATH env var)",
    )
    parser.add_argument(
        "--schemas",
        default=str(DEFAULT_SCHEMAS_DIR),
        help="Path to workflow schemas directory",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_p = subparsers.add_parser("ingest", help="Ingest a data drop directory")
    ingest_p.add_argument("drop_dir", help="Path to drop directory (e.g. data/drop_1)")

    report_p = subparsers.add_parser("report", help="Generate all 5 output report files")
    report_p.add_argument("--out", required=True, help="Output directory for reports")

    args = parser.parse_args(argv)
    conn = get_connection(args.db)
    try:
        if args.command == "ingest":
            ingest_drop(conn, Path(args.drop_dir), Path(args.schemas))
        elif args.command == "report":
            generate_reports(conn, Path(args.out))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
