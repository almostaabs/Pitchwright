"""Main pipeline: ingest -> research -> personalize -> validate -> send.

Runs unattended. Anything that passes validation is sent in this same run.

    python main.py
"""
from __future__ import annotations

import sys

from sponsor_agent import graph, sheet


def main() -> int:
    sheet.ensure_workbook()
    try:
        final = graph.run(entry="ingest")
    except FileNotFoundError as exc:
        print(f"Setup incomplete: {exc}", file=sys.stderr)
        return 2
    graph.print_summary("Pipeline run complete", final.get("sent", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
