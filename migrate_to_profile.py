"""
One-time migration: creates the first Profile row (from the old
overview.md / decision_maker.md / signature_block.md files, which the
OVERVIEW_FILE / DECISION_MAKER_FILE / SIGNATURE_BLOCK_FILE / SENDER_EMAIL
env vars used to point at) and links every existing lead and lead_run in
leads.db that doesn't already have a profile to it.

This is what let this project move from "a separate database per
user/business" (the old DATABASE_NAME env var) to "one shared database,
many Profile rows" -- see db.py's Profile model.

Safe to re-run: skips creating a duplicate profile if one with the given
email already exists, and only touches leads/lead_runs whose profile_id is
still NULL.

Usage:
    python migrate_to_profile.py                                    # uses defaults below
    python migrate_to_profile.py --email you@example.com \\
        --overview-file overview.md --decision-maker-file decision_maker.md \\
        --signature-block-file signature_block.md
"""

import argparse
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from db import DEFAULT_DB, Lead, LeadRun, Profile, get_engine

load_dotenv()

SCRIPT_DIR = Path(__file__).resolve().parent


def _read(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found.")
    return path.read_text(encoding="utf-8").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--email", default="dschellberg@gmail.com", help="Email for the seeded profile")
    parser.add_argument("--overview-file", default="overview.md", help="Path to the overview text file")
    parser.add_argument(
        "--decision-maker-file", default="decision_maker.md", help="Path to the decision-maker text file"
    )
    parser.add_argument(
        "--signature-block-file", default="signature_block.md", help="Path to the signature-block text file"
    )
    args = parser.parse_args()

    overview = _read(SCRIPT_DIR / args.overview_file)
    decision_maker = _read(SCRIPT_DIR / args.decision_maker_file)
    signature_block = _read(SCRIPT_DIR / args.signature_block_file)

    engine = get_engine(args.db)
    with Session(engine) as session:
        profile = session.query(Profile).filter(Profile.email == args.email).first()
        if profile is None:
            profile = Profile(
                email=args.email, overview=overview, decision_maker=decision_maker, signature_block=signature_block
            )
            session.add(profile)
            session.commit()
            print(f"Created profile for {args.email!r} (id={profile.id}).")
        else:
            print(f"Profile for {args.email!r} already exists (id={profile.id}) -- not creating a duplicate.")

        unlinked_leads = session.query(Lead).filter(Lead.profile_id.is_(None)).all()
        for lead in unlinked_leads:
            lead.profile_id = profile.id
        session.commit()
        print(f"Linked {len(unlinked_leads)} lead(s) with no profile to {args.email!r}.")

        unlinked_runs = session.query(LeadRun).filter(LeadRun.profile_id.is_(None)).all()
        for run in unlinked_runs:
            run.profile_id = profile.id
        session.commit()
        print(f"Linked {len(unlinked_runs)} lead_run(s) with no profile to {args.email!r}.")


if __name__ == "__main__":
    main()
