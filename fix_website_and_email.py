"""
One-time corrective script for data-quality bugs in existing rows of
leads.db, all now fixed going forward in lead_gen.py:

  1. `website` was sometimes stored as whatever subpage a search result
     happened to link to (e.g. https://acme.com/contact-us) instead of the
     company's homepage (https://acme.com).
  2. `website` was sometimes the wrong domain entirely -- a directory,
     aggregator, or news site that outranked the company's real site in the
     old Tavily-based search (e.g. a lead for "Nuuly" ended up with
     phillyvoice.com as its "website"). lead_gen.find_website() now grounds
     its pick in a Gemini web search and validates against a domain
     blocklist instead of trusting whatever ranked first.
  3. `email` was sometimes a "Contact Us" page URL, or blank, instead of a
     real email address -- and when the *website* domain was wrong (bug #2),
     any contact found was for the wrong company's domain, not this one.

What this does, per row:
  - --website-only (or no flags): purely mechanical, no API calls. Any
    stored website with a path/query/fragment beyond the bare domain is
    collapsed to just scheme://host. Cheap and always safe.
  - --rediscover-websites: re-runs a live Gemini web-search-grounded lookup
    (lead_gen.find_website) for every processed row's website, replacing
    whatever's stored -- this is what fixes bug #2 (wrong domain), not just
    #1 (wrong path). Any row whose *domain* changes as a result is also
    queued for an email re-lookup (see below), since a contact found under
    the old wrong domain is worthless.
  - Email fix (runs whenever --website-only is not passed): touches every
    processed row whose current email isn't a real address -- a URL, blank,
    or (per --rediscover-websites above) tied to a domain that just changed.
    For those, re-runs a live Gemini web-search-grounded lookup
    (lead_gen.find_contact) for a named contact matching decision_maker.md's
    description (a technical decision-maker -- CTO / VP Eng / Director of
    IT -- by default) at the row's (possibly just-corrected) domain. If a real
    email is found, it's used as-is. If only a name is found, every
    plausible email permutation of that name at the domain is generated and
    stored as a comma-separated list (guesses, not confirmed addresses --
    review before relying on any one of them). Otherwise falls back to a
    confirmed generic email, or leaves the field blank rather than storing a
    non-email value. Costs one Gemini call per row, so use --limit for a
    smoke test first.

Usage:
    python fix_website_and_email.py --website-only            # free, path-only fix, every row
    python fix_website_and_email.py --limit 5                  # smoke-test the email fix
    python fix_website_and_email.py                            # path fix + email fix (bad/blank emails only)
    python fix_website_and_email.py --rediscover-websites       # also re-pick website domains + fix affected emails
    python fix_website_and_email.py --dry-run                   # report what would change, write nothing
"""

import argparse
import time
import urllib.parse

from dotenv import load_dotenv
from google import genai
from sqlalchemy.orm import Session

from db import DEFAULT_DB, Lead, get_engine
from lead_gen import find_contact, find_website, generate_email_permutations, make_genai_client

load_dotenv()


def normalize_to_homepage(url: str) -> str:
    """scheme://host, dropping any path/query/fragment. Empty/unparseable
    input is returned unchanged."""
    if not url:
        return url
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}"


def is_bad_email(email: str) -> bool:
    """True for anything that isn't a real (comma-separated list of) email
    address (the URLs the original bug produced), OR a blank email -- a
    processed lead with no email on file is worth a fresh lookup now that
    contact search runs through Gemini's grounding instead of Tavily
    (which was both less accurate and, during the window this script's
    email-fix step first ran in, out of quota and silently failing)."""
    return not email or "@" not in email


def fix_websites(session: Session, dry_run: bool) -> int:
    leads = session.query(Lead).filter(Lead.website != "").all()
    changed = 0
    for lead in leads:
        fixed = normalize_to_homepage(lead.website)
        if fixed != lead.website:
            print(f"  website: {lead.name}: {lead.website!r} -> {fixed!r}")
            changed += 1
            if not dry_run:
                lead.website = fixed
    if not dry_run and changed:
        session.commit()
    return changed


def rediscover_website_domains(
    session: Session, client: genai.Client, limit, sleep_seconds: float, dry_run: bool
) -> tuple[int, list[Lead]]:
    """Re-picks the website for every processed lead using the new
    Gemini-grounded find_website() -- fixes wrong-domain rows (directory/
    aggregator/news sites), not just wrong-path ones. Returns (changed_count,
    leads_whose_domain_changed) -- the latter need their contact re-looked-up
    too, since any email found under the old domain no longer applies.
    """
    leads = session.query(Lead).filter(Lead.processed.is_(True)).order_by(Lead.id.asc()).all()
    if limit:
        leads = leads[:limit]

    changed = 0
    domain_changed_leads = []
    for i, lead in enumerate(leads, 1):
        print(f"  [{i}/{len(leads)}] {lead.name}")
        old_domain = urllib.parse.urlparse(lead.website).netloc.lower().removeprefix("www.")
        company = {"company name": lead.name, "city": lead.city, "state": lead.state, "description": lead.description}
        new_website, new_domain = find_website(client, company)
        new_website = new_website or ""
        if new_website != lead.website:
            print(f"    {lead.website!r} -> {new_website!r}")
            changed += 1
            if not dry_run:
                lead.website = new_website
                session.commit()
        if new_domain and new_domain != old_domain:
            domain_changed_leads.append(lead)
        time.sleep(sleep_seconds)

    return changed, domain_changed_leads


def fix_emails(session: Session, client: genai.Client, leads: list[Lead], sleep_seconds: float, dry_run: bool) -> int:
    changed = 0
    for i, lead in enumerate(leads, 1):
        print(f"  [{i}/{len(leads)}] {lead.name}")
        domain = urllib.parse.urlparse(lead.website).netloc.lower().removeprefix("www.")
        if not domain:
            print(f"    no known domain -- clearing bad email {lead.email!r}")
            new_email = ""
        else:
            company = {"company name": lead.name, "city": lead.city, "state": lead.state}
            contact = find_contact(client, company, domain)
            if contact.contact_email:
                new_email = contact.contact_email
            elif contact.contact_name:
                new_email = ", ".join(generate_email_permutations(contact.contact_name, domain))
            else:
                new_email = contact.generic_email or ""
            print(f"    {lead.email!r} -> {new_email!r}")

        if new_email != lead.email:
            changed += 1
            if not dry_run:
                lead.email = new_email
                session.commit()
        time.sleep(sleep_seconds)

    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--limit", type=int, default=None, help="Only fix the first N rows in each API-calling step")
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between API calls")
    parser.add_argument("--website-only", action="store_true", help="Only fix websites -- skip the email re-lookup")
    parser.add_argument(
        "--rediscover-websites",
        action="store_true",
        help="Also re-pick every website's domain via Gemini (fixes wrong-domain rows, not just wrong-path ones)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing to the database"
    )
    args = parser.parse_args()

    engine = get_engine(args.db)
    with Session(engine) as session:
        print("Fixing website paths...")
        website_changed = fix_websites(session, args.dry_run)
        print(f"  {website_changed} website(s) {'would be' if args.dry_run else ''} path-fixed.")

        domain_changed_leads: list[Lead] = []
        if args.rediscover_websites:
            print("\nRe-discovering website domains (live Gemini web-search-grounded lookups)...")
            client = make_genai_client()
            domains_changed, domain_changed_leads = rediscover_website_domains(
                session, client, args.limit, args.sleep, args.dry_run
            )
            print(f"  {domains_changed} website(s) {'would be' if args.dry_run else ''} domain-corrected.")

        email_changed = 0
        if not args.website_only:
            client = make_genai_client()
            bad_email_leads = (
                session.query(Lead).filter(Lead.processed.is_(True)).order_by(Lead.id.asc()).all()
            )
            targets = [lead for lead in bad_email_leads if is_bad_email(lead.email)]
            for lead in domain_changed_leads:
                if lead not in targets:
                    targets.append(lead)
            if args.limit:
                targets = targets[: args.limit]

            print(f"\nFixing emails (live Gemini web-search-grounded lookups) for {len(targets)} row(s)...")
            email_changed = fix_emails(session, client, targets, args.sleep, args.dry_run)
            print(f"  {email_changed} email(s) {'would be' if args.dry_run else ''} fixed.")

    print(f"\nDone. {website_changed} website(s), {email_changed} email(s) {'would be' if args.dry_run else ''} changed.")


if __name__ == "__main__":
    main()
