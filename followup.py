"""Second (and third, and nth) touch for prospects who never answered.

    python followup.py

Fully autonomous: it drafts, validates and sends exactly like the main
pipeline, with the same NEEDS_REVIEW exception handling. A prospect who has
replied anywhere is permanently excluded - no follow-up is ever sent into a
conversation a human has entered.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from sponsor_agent import config, graph, sheet


def _parse(timestamp) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(timestamp).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _has_touch(rows: list[dict], email: str, touch: int) -> bool:
    """Any row at all for this contact at this touch - drafted, sent or parked."""
    target = str(email or "").strip().lower()
    return any(
        str(r["contact_email"]).strip().lower() == target
        and str(r["touch_number"]) == str(touch)
        for r in rows
    )


def eligible(
    rows: list[dict], days: int | None = None, now: datetime | None = None
) -> list[dict]:
    """SENT, unanswered, old enough, and no next touch already on the sheet."""
    days = config.FOLLOWUP_DAYS if days is None else days
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    out = []
    for row in rows:
        if row["status"] != sheet.SENT:
            continue
        if sheet.has_replied(row["contact_email"], rows):
            continue
        sent_at = _parse(row["sent_at"])
        if sent_at is None or sent_at > cutoff:
            continue
        if _has_touch(rows, row["contact_email"], int(row["touch_number"] or 1) + 1):
            continue
        out.append(row)
    return out


def queue_next_touches(rows: list[dict]) -> list[dict]:
    """Append a touch_number+1 row per eligible prospect and return the run queue."""
    queue = []
    for parent in eligible(rows):
        touch = int(parent["touch_number"] or 1) + 1
        note = (
            f"follow-up: touch {parent['touch_number']} was sent "
            f"{parent['sent_at']} with no reply"
        )
        record = {
            "company": parent["company"],
            "contact_name": parent["contact_name"],
            "contact_email": parent["contact_email"],
            "website": parent["website"],
            "notes": (str(parent["notes"] or "") + " | " + note).strip(" |"),
            "research_notes": parent["research_notes"],
            "status": sheet.NEW,
            "touch_number": touch,
        }
        row_idx = sheet.append(record)
        queue.append(
            {
                **{column: record.get(column, "") for column in sheet.COLUMNS},
                "_row": row_idx,
                "_thread_id": parent["thread_id"],
                "_previous": {
                    "subject": parent["draft_subject"],
                    "body": parent["draft_body"],
                    "sent_at": parent["sent_at"],
                },
            }
        )
        sheet.log("followup", parent["company"], f"queued touch {touch}")
    return queue


def main() -> int:
    sheet.ensure_workbook()
    queue = queue_next_touches(sheet.read_rows())

    if not queue:
        print(
            "Nothing due: no SENT row is unanswered and older than "
            f"{config.FOLLOWUP_DAYS} day(s) without a next touch already queued."
        )
        return 0

    print(f"{len(queue)} follow-up(s) due.")
    try:
        final = graph.run(entry="next_prospect", queue=queue)
    except FileNotFoundError as exc:
        print(f"Setup incomplete: {exc}", file=sys.stderr)
        return 2
    graph.print_summary("Follow-up run complete", final.get("sent", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
