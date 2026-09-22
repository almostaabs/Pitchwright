"""Poll Gmail for replies to anything we sent.

    python check_replies.py

This script never writes an email. Marking a row REPLIED hands that prospect
to you permanently: the agent will not touch them again, and it will not draft
or send any answer into a thread a human has replied to.
"""
from __future__ import annotations

import sys

from sponsor_agent import gmail_client, sheet


def main() -> int:
    sheet.ensure_workbook()
    rows = sheet.read_rows()
    watched = [r for r in rows if r["status"] == sheet.SENT and str(r["thread_id"]).strip()]

    if not watched:
        print("No SENT rows with a thread id to check.")
        return 0

    try:
        me = gmail_client.my_address()
    except Exception as exc:
        print(f"Could not reach Gmail: {exc}", file=sys.stderr)
        return 2

    found = 0
    for row in watched:
        company = row["company"]
        try:
            reply = gmail_client.find_reply(str(row["thread_id"]), me)
        except Exception as exc:
            sheet.log("check_replies", company, f"FAILED: {type(exc).__name__}: {exc}")
            continue

        if not reply:
            continue

        sheet.update(
            row["_row"],
            status=sheet.REPLIED,
            replied_at=reply["received_at"] or sheet.now(),
            reply_snippet=reply["snippet"],
            last_error="",
        )
        sheet.log(
            "check_replies",
            company,
            f"REPLIED by {reply['from']} - handed over, no automated response will be sent",
        )
        print(f"REPLIED  {company} <{row['contact_email']}>: {reply['snippet'][:120]}")
        found += 1

    print(f"\nChecked {len(watched)} thread(s), found {found} new repl(y/ies).")
    if found:
        print("These are live conversations now. Reply to them yourself in Gmail.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
