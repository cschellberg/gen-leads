"""
Parses LinkedIn company-search result page text (as fetched via a browser
session -- never by an automated fetcher in this file) and loads new
companies into the leads table, logging the page range in lead_runs so it's
always clear what's already been covered.

This module deliberately does NOT fetch anything from LinkedIn itself. The
expected workflow is: browse to a page (a human, or an agent driving a real
browser session with a human aware of it), get that page's visible text,
and hand it to ingest_linkedin_page_text() here. Each new company lands in
`leads` with processed=False -- lead_gen.py's enrichment pass (Process)
picks those up later.

Usage:
    from linkedin_import import ingest_linkedin_page_text
    from db import get_engine, DEFAULT_DB
    from sqlalchemy.orm import Session

    with Session(get_engine(DEFAULT_DB)) as session:
        added = ingest_linkedin_page_text(
            session, url=SEARCH_URL, from_page=11, to_page=11, raw_text=page_text
        )
"""

import re

from db import Lead, LeadRun

FOLLOWERS_RE = re.compile(r".*\bfollowers?$", re.IGNORECASE)
TRAILING_LABELS = {"visit website", "visit store", "learn more", "register", "contact us"}

# US state name -> abbreviation, for turning "Philadelphia, Pennsylvania"
# into city="Philadelphia", state="PA" the same way the original
# philly_companies.csv locations were normalized.
STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}


def parse_location(loc: str) -> tuple[str, str]:
    loc = loc.strip()
    if "|" in loc:  # multi-location entries (e.g. "Chicago | Dallas | ...")
        return loc, ""
    parts = [p.strip() for p in loc.split(",")]
    if len(parts) < 2:
        return parts[0], ""
    city, state_raw = parts[0], parts[1].strip()
    if len(state_raw) == 2:
        return city, state_raw.upper()
    return city, STATE_ABBR.get(state_raw.lower(), state_raw)


def parse_linkedin_page_text(text: str) -> list[dict]:
    """Extracts company entries from a LinkedIn company-search results
    page's visible text. Each entry in the real page is laid out as:

        <Name>
        <Industry>
        <Location>
        Follow
        <description paragraph(s)>
        <"NNK followers" or "X & N others follow this page - NNK followers">
        [optional: Visit website / Learn more / Register / Contact us]

    with a blank line between every field. "Follow" and the trailing
    followers-count line are used as anchors, so surrounding page chrome
    (result counts, ads, pagination controls, footer links) that doesn't
    fit this shape is naturally skipped rather than mis-parsed.
    """
    blocks = [b.strip() for b in text.split("\n\n")]
    blocks = [b for b in blocks if b]

    entries = []
    i = 0
    n = len(blocks)
    while i < n:
        if blocks[i] == "Follow" and i >= 3:
            name, industry, location = blocks[i - 3], blocks[i - 2], blocks[i - 1]
            j = i + 1
            desc_parts = []
            while j < n and not FOLLOWERS_RE.match(blocks[j]):
                desc_parts.append(blocks[j])
                j += 1
            if j < n:
                j += 1  # consume the followers-count line itself
            while j < n and blocks[j].strip().lower() in TRAILING_LABELS:
                j += 1
            city, state = parse_location(location)
            entries.append(
                {
                    "name": name,
                    "industry": industry,
                    "city": city,
                    "state": state,
                    "description": " ".join(desc_parts),
                }
            )
            i = j
        else:
            i += 1
    return entries


def _normalize_name(name: str) -> str:
    """For dedup comparison only (never used as the stored name): LinkedIn
    renders the same company's trademark/registered symbols inconsistently
    across pages (seen in practice: "Fatpos Global" on one page,
    "Fatpos Global™" on another), which would otherwise slip past an
    exact-match dedup check as a false "new" company.
    """
    return re.sub(r"[™®©]", "", name).strip().lower()


def ingest_linkedin_page_text(session, url: str, from_page: int, to_page: int, raw_text: str) -> int:
    """Parses raw_text and adds any new companies to `leads` (processed=False,
    skipping names already present -- same idempotency rule as lead_gen.py),
    then logs one lead_runs row for this page range regardless of how many
    (if any) were new. Returns the number of companies actually added.
    """
    entries = parse_linkedin_page_text(raw_text)
    existing_names = {_normalize_name(name) for (name,) in session.query(Lead.name).all()}

    added = 0
    for entry in entries:
        key = _normalize_name(entry["name"])
        if key in existing_names:
            continue
        session.add(
            Lead(
                name=entry["name"],
                city=entry["city"],
                state=entry["state"],
                description=entry["description"],
            )
        )
        existing_names.add(key)
        added += 1

    session.add(LeadRun(url=url, from_page=from_page, to_page=to_page))
    session.commit()
    return added
