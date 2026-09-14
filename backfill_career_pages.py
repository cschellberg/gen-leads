"""
One-time backfill for the `career_page` column (added after most existing
leads were already processed): looks up each targeted lead's careers/jobs
page via the same Gemini web-search-grounded lookup lead_gen.py now runs
during normal enrichment (lead_gen.find_career_page), and stores the result.

Only touches processed leads that don't already have a career_page (unless
--force), scoped to one Profile's leads by email via --profile-email.

Usage:
    python backfill_career_pages.py --profile-email mschellberg12@gmail.com
    python backfill_career_pages.py --profile-email mschellberg12@gmail.com --limit 5   # smoke test
    python backfill_career_pages.py --profile-email mschellberg12@gmail.com --dry-run
    python backfill_career_pages.py --profile-email mschellberg12@gmail.com --force      # re-check every row, even ones already set
"""

import argparse
import time

from sqlalchemy.orm import Session

from db import DEFAULT_DB, Lead, Profile, get_engine
from lead_gen import find_career_page, make_genai_client


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--profile-email", required=True, help="Only backfill leads linked to this Profile's email")
    parser.add_argument("--limit", type=int, default=None, help="Only backfill the first N matching leads")
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between API calls")
    parser.add_argument(
        "--force", action="store_true", help="Re-check every matching row, even ones that already have a career_page"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing to the database"
    )
    args = parser.parse_args()

    engine = get_engine(args.db)
    with Session(engine) as session:
        profile = session.query(Profile).filter(Profile.email == args.profile_email).one_or_none()
        if profile is None:
            raise SystemExit(f"No Profile found with email {args.profile_email!r}.")

        query = session.query(Lead).filter(Lead.profile_id == profile.id, Lead.processed.is_(True))
        if not args.force:
            query = query.filter(Lead.career_page == "")
        leads = query.order_by(Lead.id.asc()).all()
        if args.limit:
            leads = leads[: args.limit]

        print(f"{len(leads)} lead(s) to check for {args.profile_email}.")
        client = make_genai_client()

        changed = 0
        for i, lead in enumerate(leads, 1):
            company = {
                "company name": lead.name,
                "city": lead.city,
                "state": lead.state,
                "description": lead.description,
            }
            domain = lead.website.replace("https://", "").replace("http://", "").split("/")[0] or None
            career_page = find_career_page(client, company, domain) or ""
            print(f"  [{i}/{len(leads)}] {lead.name}: {lead.career_page!r} -> {career_page!r}")
            if career_page != lead.career_page:
                changed += 1
                if not args.dry_run:
                    lead.career_page = career_page
                    session.commit()
            time.sleep(args.sleep)

    print(f"\nDone. {changed} lead(s) {'would be' if args.dry_run else ''} updated.")


if __name__ == "__main__":
    main()
