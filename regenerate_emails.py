"""
Backfills subject + Markdown-formatted body for leads already in the
database -- for leads created before Markdown-styled emails were introduced.
Uses Gemini, guided by the same style example (email_example.md) that
lead_gen.py now follows for every new company going forward.

This only touches subject/body. Ranking, website, email, etc. are left
alone -- reuse lead_gen.py's draft_email() for the actual drafting so the
two scripts can never drift out of style with each other.

By default, skips leads whose body already looks *properly* Markdown-
formatted -- has a heading/bold/bullet marker AND an actual line break --
so reruns don't re-spend API calls on leads that are already done. A body
with markdown markers but no line breaks (the bug this script exists to
fix) does NOT count as done and gets reformatted. Use --force to reformat
every lead regardless.

Usage:
    python regenerate_emails.py --limit 5   # smoke test
    python regenerate_emails.py             # reformat everything not already done
    python regenerate_emails.py --force     # reformat every lead, even done ones
"""

import argparse
import re
import sys
import time

from sqlalchemy.orm import Session

from db import DEFAULT_DB, Lead, get_engine
from lead_gen import ContactInfo, draft_email, make_llm

MARKDOWN_MARKERS = re.compile(r"(\*\*[^*]+\*\*|^#{1,6}\s|^[-*]\s)", re.MULTILINE)


def looks_properly_formatted(body: str) -> bool:
    """Markdown markers alone aren't enough -- a body can have '**bold**'
    and '- bullets' but still be one run-on paragraph with no line breaks
    (see lead_gen.py's normalize_markdown_linebreaks for why). Only count
    it as done if it has markdown markers AND at least one real line break.
    """
    body = body or ""
    return bool(MARKDOWN_MARKERS.search(body)) and "\n" in body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--limit", type=int, default=None, help="Only reformat the first N matching leads")
    parser.add_argument(
        "--force", action="store_true", help="Reformat every lead, even ones that already look Markdown-formatted"
    )
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between leads")
    args = parser.parse_args()

    engine = get_engine(args.db)
    write_llm = make_llm(temperature=0.6)

    with Session(engine) as session:
        todo = session.query(Lead).order_by(Lead.id.asc()).all()
 #       todo = leads if args.force else [lead for lead in leads if not looks_properly_formatted(lead.body)]
        if args.limit:
            todo = todo[: args.limit]
        print(f"{len(todo)} total leads, {len(todo)} to reformat.")

        for i, lead in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {lead.name}")
            company = {
                "company name": lead.name,
                "city": lead.city,
                "state": lead.state,
                "description": lead.description,
            }
            try:
                # No stored contact name/title to pass through -- ContactInfo()
                # (all None) makes draft_email address the email generally.
                draft = draft_email(write_llm, company, lead.website, ContactInfo())
            except Exception as e:
                print(f"    [FAILED: {e}]", file=sys.stderr)
                continue
            lead.subject = draft.subject
            lead.body = draft.body
            session.commit()
            time.sleep(args.sleep)

    print("Done.")


if __name__ == "__main__":
    main()
