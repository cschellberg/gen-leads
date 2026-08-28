"""
Lead-enrichment pipeline for Succinct Solutions ("Process" in the standalone
app). For every lead in the database with processed=False -- added by
linkedin_import.py after a scrape, from any other source -- this:
  1. Searches the web (Tavily) to find the company's official website --
     stored as just the homepage (scheme + domain), never a subpage.
  2. Searches for a named technical decision-maker (CTO / VP Eng / Director of
     IT / Head of Technology) and their email. If a real email is confirmed
     in search results, that's used verbatim. If only the person's name is
     found (no email), every plausible email permutation of their name at
     the company's domain is generated (first.last@, flast@, etc.) and
     stored as a comma-separated list -- these are guesses, not confirmed
     addresses, so review before relying on any single one. Falls back to a
     generic info@/contact@ address found via search when no named contact
     can be confirmed. The email field is always in standard email-address
     form -- never a "Contact Us" page URL.
  3. Scores the company 1-10 on how likely it is to need Succinct Solutions'
     services (full-stack dev + graphic design, 1099 contract work).
  4. Drafts a short, tailored cold-outreach email (subject + Markdown-formatted
     body), following the style/structure of email_example.md.
  5. Writes the results back onto that same row and sets processed=True.

Results are stored in the SQLite database (one table, "leads") via the
SQLAlchemy ORM model defined in db.py -- import from there (not from this
file) if another app needs to read/write the same table. Idempotent by
construction: a lead is only ever picked up while processed=False, and gets
marked True (even on failure, so a permanently-broken lead doesn't get
retried -- and re-billed -- forever) before moving on, so rerunning the
script never reprocesses a lead you already have results for.

This script only DRAFTS emails -- it never sends anything. Review contacts
and copy before using them; automated search can miss context, and outbound
commercial email is subject to CAN-SPAM (truthful subject/header, clear
sender ID, working opt-out, physical address) so review before sending.

Usage:
    python lead_gen.py --limit 5   # smoke test on the first 5 unprocessed leads
    python lead_gen.py             # process every unprocessed lead
"""

import argparse
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import DEFAULT_DB, Lead, get_engine

load_dotenv()

GEMINI_MODEL = "gemini-3.1-flash-lite"  # cheapest current Gemini chat model
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Resolved relative to this file (not the current working directory), so the
# script finds its data whether you run it from here or from the project root.
SCRIPT_DIR = Path(__file__).resolve().parent
EMAIL_EXAMPLE_PATH = SCRIPT_DIR / "email_example.md"

_email_example_cache: Optional[str] = None


def get_email_example() -> str:
    """The Markdown style example every drafted email should follow (loaded
    once and cached). Raises a clear error if the file is missing rather
    than silently drafting unstyled emails."""
    global _email_example_cache
    if _email_example_cache is None:
        if not EMAIL_EXAMPLE_PATH.exists():
            raise FileNotFoundError(
                f"{EMAIL_EXAMPLE_PATH} not found. This file is the Markdown style/formatting "
                "example drafted emails follow -- create it before running."
            )
        _email_example_cache = EMAIL_EXAMPLE_PATH.read_text(encoding="utf-8")
    return _email_example_cache

# Domains that show up in "company official website" searches but are never
# the company's own site -- skip these when picking the website URL.
NON_OFFICIAL_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "indeed.com", "glassdoor.com", "zoominfo.com", "bloomberg.com",
    "crunchbase.com", "yelp.com", "bbb.org", "wikipedia.org", "youtube.com",
    "google.com", "maps.google.com", "apollo.io", "pitchbook.com",
    "owler.com", "signalhire.com", "rocketreach.co", "builtin.com",
}

SUCCINCT_SOLUTIONS_BLURB = """\
Succinct Solutions (succinctsolutions.net) is a US-based full-stack software \
development and design shop. Positioning: "Delivering smart, efficient \
solutions that are easy to understand, scale, and support, long after \
launch" -- built to avoid the over-engineered, bloated software that's hard \
to maintain. Led by a Senior Software Engineer/Architect with 24+ years of \
hands-on experience. Stack: Java, Python, Spring, Node.js, AWS, Kubernetes, \
serverless architectures, and cloud migrations. Also does graphic/UX design. \
Differentiators: custom-tailored architecture (not off-the-shelf), designed \
to handle traffic growth without re-engineering, and ongoing post-launch \
support ("our work doesn't end at hand-off"). Being US-based means \
straightforward IRS-compliant 1099 contracting with no international \
vendor complexity. Offers discounted bill rates for the right-fit \
contract/1099 engagements.\
"""

EMAIL_CLOSING = (
    # Two trailing spaces before each \n is Markdown's hard-break syntax --
    # without it, a plain single \n between lines that aren't list items or
    # separated by a blank line is just a soft wrap, and any real Markdown
    # renderer/converter collapses these lines into one, which is what "it
    # loses its formatting when I convert it" was actually about.
    "Best,  \n"
    "Donald Schellberg  \n"
    "Principal Systems Architect, Succinct Solutions  \n"
    "dschellberg@gmail.com | 484-688-3233  \n"
    "https://succinctsolutions.net"
)

BASE_SYSTEM_PROMPT = (
    "You are a small US based IT tech company that does full stack "
    "programming and graphic design. You are looking for 1099 contracts "
    "and you offer discounted bill rates"
)


class ContactInfo(BaseModel):
    contact_name: Optional[str] = Field(
        None,
        description="Full name of a technical decision-maker (CTO, VP of "
        "Engineering, Head of Technology, Director of IT/Engineering, etc.) "
        "ONLY if it appears explicitly in the provided search results. "
        "Null if not found.",
    )
    contact_title: Optional[str] = Field(
        None, description="That person's job title, only if stated in the search results."
    )
    contact_email: Optional[str] = Field(
        None,
        description="That person's direct email address, ONLY if it appears "
        "verbatim in the search results. Never guess, infer, or construct an "
        "email address from a name/domain pattern.",
    )
    generic_email: Optional[str] = Field(
        None,
        description="A general company contact email (e.g. info@, "
        "contact@, hello@) ONLY if it appears verbatim in the search "
        "results. Never invent one.",
    )


class LeadAssessment(BaseModel):
    ranking: int = Field(
        ge=1,
        le=10,
        description="1-10 score for how likely this company is to need "
        "Succinct Solutions' full-stack dev / graphic design 1099 contract "
        "services. 1 = least likely, 10 = most likely.",
    )
    subject: str = Field(description="Email subject line, tailored to this company, under 80 characters.")
    body: str = Field(
        description="The email's message content only -- do NOT include a "
        "greeting/salutation or a closing/signoff/signature, both are "
        "appended separately afterward. In Markdown: MUST use real newline "
        "characters ('\\n') between paragraphs, before/after headings, and "
        "between each bullet list item -- never run separate lines together "
        "into one paragraph, even if markdown syntax like '- ' is present. "
        "Follows the structure and formatting of the provided style example. "
        "Professional cold-outreach tone, 100-180 words."
    )


def make_llm(temperature: float) -> ChatOpenAI:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key :
        sys.exit(
            "GOOGLE_API_KEY in .env is missing or still a placeholder. "
            "Set a real Gemini API key (https://aistudio.google.com/apikey) and try again."
        )
    return ChatOpenAI(
        model=GEMINI_MODEL,
        base_url=GEMINI_BASE_URL,
        api_key=api_key,
        temperature=temperature,
    )


def find_website(search: TavilySearch, company: dict) -> tuple[Optional[str], Optional[str]]:
    """Returns (homepage_url, bare_domain). The homepage URL is always just
    scheme + host -- e.g. a search result of https://acme.com/contact-us
    becomes https://acme.com -- never a subpage, since the stored website
    should be the company's front door, not whatever page happened to rank.
    """
    query = f"{company['company name']} {company['city']} {company['state']} official website"
    try:
        result = search.invoke({"query": query})
    except Exception as e:
        print(f"    [website search failed: {e}]", file=sys.stderr)
        return None, None
    for r in result.get("results", []):
        url = r.get("url", "")
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower().removeprefix("www.")
        is_blocked = any(domain == d or domain.endswith("." + d) for d in NON_OFFICIAL_DOMAINS)
        if domain and parsed.scheme in ("http", "https") and not is_blocked:
            return f"{parsed.scheme}://{parsed.netloc}", domain
    return None, None


def generate_email_permutations(full_name: str, domain: str) -> list[str]:
    """Every plausible corporate email pattern for a person's name at a
    given domain (first.last@, flast@, etc.) -- used as a fallback guess
    list when a name is known but no real email was found in search
    results. These are guesses, never confirmed addresses.
    """
    tokens = re.findall(r"[A-Za-z]+", full_name)
    if len(tokens) < 2:
        if tokens:
            return [f"{tokens[0].lower()}@{domain}"]
        return []
    first, last = tokens[0].lower(), tokens[-1].lower()
    f, l = first[0], last[0]
    candidates = [
        f"{first}.{last}@{domain}",
        f"{first}{last}@{domain}",
        f"{first}_{last}@{domain}",
        f"{first}-{last}@{domain}",
        f"{f}{last}@{domain}",
        f"{last}.{first}@{domain}",
        f"{last}{f}@{domain}",
        f"{first}@{domain}",
    ]
    return list(dict.fromkeys(candidates))  # de-dupe, preserve order


def gather_contact_snippets(search: TavilySearch, company: dict, domain: Optional[str]) -> str:
    name = company["company name"]
    queries = []
    if domain:
        queries.append(
            {"query": "leadership team CTO VP Engineering Director of IT contact email", "include_domains": [domain]}
        )
        queries.append({"query": "contact us email", "include_domains": [domain]})
    queries.append(
        {"query": f'"{name}" CTO OR "VP of Engineering" OR "Head of Technology" OR "Director of IT" email contact'}
    )

    chunks = []
    for q in queries:
        try:
            result = search.invoke(q)
        except Exception as e:
            print(f"    [contact search failed: {e}]", file=sys.stderr)
            continue
        for r in result.get("results", [])[:4]:
            content = (r.get("content") or "")[:600]
            chunks.append(f"URL: {r.get('url')}\nTITLE: {r.get('title')}\nCONTENT: {content}")
    return "\n\n---\n\n".join(chunks)[:6000]


def extract_contact(extract_llm: ChatOpenAI, company: dict, snippets: str) -> ContactInfo:
    if not snippets.strip():
        return ContactInfo()
    structured = extract_llm.with_structured_output(ContactInfo)
    prompt = (
        f"Company: {company['company name']} ({company['city']}, {company['state']})\n\n"
        "Below are web search results about this company. Extract a technical "
        "decision-maker's name/title/email if explicitly present, otherwise a "
        "generic company email. Only use facts that literally appear below -- "
        "never guess or construct an email address.\n\nSEARCH RESULTS:\n" + snippets
    )
    try:
        return structured.invoke(prompt)
    except Exception as e:
        print(f"    [contact extraction failed: {e}]", file=sys.stderr)
        return ContactInfo()


def normalize_markdown_linebreaks(text: str) -> str:
    """Defensive safety net: even with explicit instructions, models can
    still emit valid Markdown *syntax* (headings, bold, bullets) but omit
    the actual line breaks Markdown needs to render as separate blocks/list
    items, producing one run-on paragraph. Insert a line break before
    headings and "- **bold**"-style list items when they don't already
    start on their own line. Deliberately conservative (only matches
    unambiguous markers) so it can't mangle ordinary prose.
    """
    if not text:
        return text
    text = re.sub(r"(?<!\n)(#{1,6} )", r"\n\n\1", text)  # headings
    text = re.sub(r"(?<!\n)([-*] \*\*)", r"\n\1", text)  # "- **bold**" list items
    text = re.sub(r"\n{3,}", "\n\n", text)  # tidy up any resulting triple+ blank lines
    return text.strip()


def build_greeting(contact: ContactInfo) -> str:
    """The greeting is decided in code, not by the model -- we already know
    deterministically whether a named contact was found, so there's no
    reason to leave it to prompt-following (which is exactly what went
    wrong with the closing/signature)."""
    if contact.contact_name:
        return f"Hi {contact.contact_name},"
    return "Greetings,"


def draft_email(write_llm: ChatOpenAI, company: dict, website: Optional[str], contact: ContactInfo) -> LeadAssessment:
    structured = write_llm.with_structured_output(LeadAssessment)
    contact_desc = (
        f"Named contact: {contact.contact_name} ({contact.contact_title})"
        if contact.contact_name
        else "No named contact identified."
    )
    human = (
        f"{SUCCINCT_SOLUTIONS_BLURB}\n\n"
        "Prospect company:\n"
        f"- Name: {company['company name']}\n"
        f"- Location: {company['city']}, {company['state']}\n"
        f"- Description: {company['description']}\n"
        f"- Website: {website or 'unknown'}\n"
        f"- {contact_desc}\n\n"
        "Task 1 -- Rank this company 1-10 on how likely it is to need Succinct "
        "Solutions' full-stack dev / graphic design 1099 contract services. "
        "Score HIGH (7-10) for small-to-mid-sized companies that plausibly need "
        "custom software, web, or design work and don't appear to already be a "
        "software/IT/dev agency themselves. Score LOW (1-3) for: other IT/software "
        "development or staffing/recruiting agencies (peers, not buyers), and very "
        "large, well-resourced organizations (major sports franchises, big VC "
        "firms, universities, national nonprofits) that likely use enterprise "
        "vendors or have large internal teams already. Use the description as your "
        "main signal; if it's genuinely ambiguous, score in the 4-6 range.\n\n"
        "Task 2 -- Write a short, specific cold-outreach email (subject + "
        "Markdown-formatted body) from Succinct Solutions offering full-stack "
        "development and graphic design services on a 1099 contract basis with "
        "discounted bill rates. Follow the STYLE, STRUCTURE, and Markdown "
        "formatting of the example below -- it's a style reference only, don't "
        "copy its specific wording or claims. Do NOT write a greeting/salutation "
        "at the start (e.g. no 'Hi there,' line) and do NOT write a closing/"
        "signoff/signature at the end (e.g. no 'Best,' line, no name) -- both "
        "get added automatically before/after your text, so the body should "
        "start directly with the first content sentence and end after the "
        "last one.\n\n"
        "CRITICAL FORMATTING RULE: the body must use REAL newline characters "
        "('\\n') to separate paragraphs, put each bullet list item on its own "
        "line, and put headings on their own line. Markdown syntax like '- ' "
        "is not enough on its own if everything runs together without line "
        "breaks -- look at how the example below is broken into separate "
        "lines and match that exactly, don't collapse it into one paragraph.\n\n"
        "Reference something specific about the company from its description. "
        "Do not fabricate facts about the recipient company. Do not claim a "
        "referral or prior relationship.\n\nSTYLE EXAMPLE (reference only):\n---\n"
        + get_email_example()
        + "\n---"
    )
    try:
        result = structured.invoke([("system", BASE_SYSTEM_PROMPT), ("human", human)])
        greeting = build_greeting(contact)
        result.body = greeting + "\n\n" + normalize_markdown_linebreaks(result.body) + "\n\n" + EMAIL_CLOSING
        return result
    except Exception as e:
        print(f"    [email drafting failed: {e}]", file=sys.stderr)
        return LeadAssessment(ranking=1, subject="", body="")


def process_company(
    search: TavilySearch, extract_llm: ChatOpenAI, write_llm: ChatOpenAI, company: dict
) -> dict:
    name = company["company name"]
    print(f"  -> {name}")

    website, domain = find_website(search, company)
    snippets = gather_contact_snippets(search, company, domain)
    contact = extract_contact(extract_llm, company, snippets)

    if contact.contact_email:
        email = contact.contact_email
    elif contact.contact_name and domain:
        email = ", ".join(generate_email_permutations(contact.contact_name, domain))
    else:
        email = contact.generic_email or ""

    assessment = draft_email(write_llm, company, website, contact)

    return {
        "name": name,
        "city": company["city"],
        "state": company["state"],
        "description": company["description"],
        "ranking": assessment.ranking,
        "website": website or "",
        "email": email,
        "subject": assessment.subject,
        "body": assessment.body,
    }


def process_unprocessed_leads(
    session: Session,
    search: TavilySearch,
    extract_llm: ChatOpenAI,
    write_llm: ChatOpenAI,
    limit: Optional[int] = None,
    sleep_seconds: float = 1.0,
    on_progress=None,
) -> int:
    """Enriches every lead with processed=False in place (website, email,
    ranking, subject, body) and marks it processed=True -- on success or
    failure alike, so a permanently-broken lead is never retried forever.

    on_progress, if given, is called as on_progress(i, total, lead) before
    each lead is processed -- lets a GUI show live status without this
    function needing to know anything about GUIs.

    Returns the number of leads processed in this call.
    """
    leads = session.query(Lead).filter(Lead.processed.is_(False)).order_by(Lead.id.asc()).all()
    if limit:
        leads = leads[:limit]

    for i, lead in enumerate(leads, 1):
        if on_progress:
            on_progress(i, len(leads), lead)
        company = {
            "company name": lead.name,
            "city": lead.city,
            "state": lead.state,
            "description": lead.description,
        }
        try:
            row = process_company(search, extract_llm, write_llm, company)
            lead.ranking = row["ranking"]
            lead.website = row["website"]
            lead.email = row["email"]
            lead.subject = row["subject"]
            lead.body = row["body"]
        except Exception as e:
            print(f"    [FAILED: {e}]", file=sys.stderr)
        lead.processed = True
        session.commit()
        time.sleep(sleep_seconds)

    return len(leads)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N unprocessed leads")
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between leads")
    args = parser.parse_args()

    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_key:
        sys.exit("TAVILY_API_KEY is not set in .env. Get a free key at https://app.tavily.com and add it.")

    engine = get_engine(args.db)
    search = TavilySearch(max_results=5, search_depth="basic")
    extract_llm = make_llm(temperature=0)
    write_llm = make_llm(temperature=0.6)

    def report(i, total, lead):
        print(f"[{i}/{total}] {lead.name}")

    with Session(engine) as session:
        remaining_before = session.query(Lead).filter(Lead.processed.is_(False)).count()
        print(f"{remaining_before} unprocessed leads.")
        done = process_unprocessed_leads(
            session, search, extract_llm, write_llm, limit=args.limit, sleep_seconds=args.sleep, on_progress=report
        )
        remaining_after = session.query(Lead).filter(Lead.processed.is_(False)).count()

    print(f"Done. Processed {done} lead(s). {remaining_after} unprocessed remain in {args.db}.")


if __name__ == "__main__":
    main()
