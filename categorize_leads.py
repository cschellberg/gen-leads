"""
One-time backfill script: assigns an industry `category` (one of
db.CATEGORIES) to every processed lead that predates that column, i.e.
still has category="". Going forward, new leads get a category
automatically during lead_gen.py's enrichment pipeline (as part of
draft_email()'s structured output) -- this script only needs to be run
once to fill in the leads that were already in the database before the
column existed.

Categorizing from just the company's name + description doesn't need a live
web search, so this uses lead_gen.make_llm() (the plain OpenAI-compat
Gemini client used for drafting) rather than the web-search-grounded client
used for website/contact lookups -- cheaper and faster.

Usage:
    python categorize_leads.py --limit 5   # smoke test
    python categorize_leads.py             # backfill every uncategorized processed lead
    python categorize_leads.py --dry-run   # report what would change, write nothing
"""

import argparse
import sys
import time
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import CATEGORIES, DEFAULT_DB, Lead, get_engine
from lead_gen import make_llm

load_dotenv()


class CategoryPick(BaseModel):
    category: Literal[*CATEGORIES] = Field(
        description="The single best-fit industry category for this company. "
        "Pick 'Other' only if none of the rest plausibly fit."
    )


def categorize_leads(session: Session, limit, sleep_seconds: float, dry_run: bool) -> int:
    leads = (
        session.query(Lead)
        .filter(Lead.processed.is_(True))
        .filter(Lead.category == "")
        .order_by(Lead.id.asc())
        .all()
    )
    if limit:
        leads = leads[:limit]

    llm = make_llm(temperature=0)
    structured = llm.with_structured_output(CategoryPick)

    changed = 0
    for i, lead in enumerate(leads, 1):
        print(f"  [{i}/{len(leads)}] {lead.name}")
        prompt = (
            f"Company: {lead.name}\n"
            f"Location: {lead.city}, {lead.state}\n"
            f"Description: {lead.description}\n\n"
            "Pick the single best-fit industry category for this company from the allowed list."
        )
        try:
            category = structured.invoke(prompt).category
        except Exception as e:
            print(f"    [categorization failed: {e}]", file=sys.stderr)
            continue

        print(f"    -> {category}")
        changed += 1
        if not dry_run:
            lead.category = category
            session.commit()
        time.sleep(sleep_seconds)

    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--limit", type=int, default=None, help="Only categorize the first N uncategorized rows")
    parser.add_argument("--sleep", type=float, default=0.5, help="Seconds to sleep between API calls")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing to the database"
    )
    args = parser.parse_args()

    engine = get_engine(args.db)
    with Session(engine) as session:
        print("Categorizing leads...")
        changed = categorize_leads(session, args.limit, args.sleep, args.dry_run)

    print(f"\nDone. {changed} lead(s) {'would be' if args.dry_run else ''} categorized.")


if __name__ == "__main__":
    main()
