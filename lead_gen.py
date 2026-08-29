"""
Lead-enrichment pipeline for Succinct Solutions ("Process" in the standalone
app). For every lead in the database with processed=False -- added by
linkedin_import.py after a scrape, from any other source -- this:
  1. Uses Gemini's native web-search grounding (google-genai, NOT Tavily --
     see below) to find the company's official website -- stored as just the
     homepage (scheme + domain), never a subpage.
  2. Searches for a named contact matching decision_maker.md's description
     (a technical decision-maker -- CTO / VP Eng / Director of IT / Head of
     Technology -- by default, but editable per business) and their email.
     If a real email is confirmed
     in search results, that's used verbatim. If only the person's name is
     found (no email), every plausible email permutation of their name at
     the company's domain is generated (first.last@, flast@, etc.) and
     stored as a comma-separated list -- these are guesses, not confirmed
     addresses, so review before relying on any single one. Falls back to a
     generic info@/contact@ address found via search when no named contact
     can be confirmed. The email field is always in standard email-address
     form -- never a "Contact Us" page URL.
  3. Scores the company 1-10 on how likely it is to need Succinct Solutions'
     services (full-stack dev + graphic design, 1099 contract work), and
     tags it with an industry category from db.CATEGORIES.
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

Search backend: website and contact lookups both use Gemini's native web
search grounding (the "google_search" tool via the google-genai SDK, talking
to the same generativelanguage.googleapis.com API as everything else here,
just not through the OpenAI-compat layer -- Google Search grounding isn't
exposed there). This replaced an earlier Tavily-based implementation.
Drafting (ranking + subject/body) is unrelated to search and still goes
through langchain_openai.ChatOpenAI against Gemini's OpenAI-compat endpoint,
same as before.

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
from typing import Literal, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import CATEGORIES, DEFAULT_DB, Lead, get_engine

load_dotenv()

GEMINI_MODEL = "gemini-3.1-flash-lite"  # cheapest current Gemini chat model
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Resolved relative to this file (not the current working directory), so the
# script finds its data whether you run it from here or from the project root.
SCRIPT_DIR = Path(__file__).resolve().parent
EMAIL_EXAMPLE_PATH = SCRIPT_DIR / "email_example.md"
SIGNATURE_BLOCK_PATH = SCRIPT_DIR / "signature_block.md"
OVERVIEW_PATH = SCRIPT_DIR / "overview.md"
DECISION_MAKER_PATH = SCRIPT_DIR / "decision_maker.md"

_email_example_cache: Optional[str] = None
_signature_block_cache: Optional[str] = None
_overview_blurb_cache: Optional[str] = None
_decision_maker_cache: Optional[str] = None


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


def get_overview_blurb() -> str:
    """The description of your company/offering (OVERVIEW_BLURB) fed into
    both the fit-ranking and email-drafting prompts (loaded once and
    cached). Kept in its own file, separate from code, so it can be
    tailored per user/business without touching lead_gen.py -- edit
    overview.md and re-run regenerate_emails.py to backfill existing leads.
    However the file happens to be line-wrapped, it's collapsed into one
    flowing paragraph (matching the old hardcoded string's behavior) so
    editor word-wrap never affects the actual prompt text. Raises a clear
    error if the file is missing rather than silently drafting emails with
    no company description.
    """
    global _overview_blurb_cache
    if _overview_blurb_cache is None:
        if not OVERVIEW_PATH.exists():
            raise FileNotFoundError(
                f"{OVERVIEW_PATH} not found. This file describes your company/offering and "
                "is used in every lead's fit-ranking and drafted email -- create it before running."
            )
        _overview_blurb_cache = " ".join(OVERVIEW_PATH.read_text(encoding="utf-8").split())
    return _overview_blurb_cache


def get_decision_maker_description() -> str:
    """The noun phrase describing who counts as the target contact at a
    prospect company (e.g. "a technical decision-maker (CTO, VP of
    Engineering, Head of Technology, or Director of IT/Engineering)") --
    drops directly into find_contact()'s "Search the web for ___ at this
    company" prompt. Kept in its own file so a different user/business can
    retarget contact search at a different kind of decision-maker (e.g.
    marketing, finance, operations) without touching lead_gen.py -- edit
    decision_maker.md. Loaded once and cached; however it's line-wrapped in
    the file, it's collapsed into one flowing phrase. Raises a clear error
    if the file is missing rather than silently searching for no one in
    particular.
    """
    global _decision_maker_cache
    if _decision_maker_cache is None:
        if not DECISION_MAKER_PATH.exists():
            raise FileNotFoundError(
                f"{DECISION_MAKER_PATH} not found. This file describes who counts as the "
                "target contact at a prospect company -- create it before running."
            )
        _decision_maker_cache = " ".join(DECISION_MAKER_PATH.read_text(encoding="utf-8").split())
    return _decision_maker_cache


def get_signature_block() -> str:
    """The Markdown closing/signature appended to every drafted email (loaded
    once and cached). Kept in its own file rather than a Python constant so
    the signature can be changed (name, title, contact info) without
    touching code -- edit signature_block.md and re-run regenerate_emails.py
    to backfill existing leads. Each line but the last must end with two
    trailing spaces (Markdown's hard-break syntax) to render as separate
    lines instead of one run-on paragraph -- see email_example.md's note on
    this same point. Raises a clear error if the file is missing rather than
    silently drafting emails with no signature.
    """
    global _signature_block_cache
    if _signature_block_cache is None:
        if not SIGNATURE_BLOCK_PATH.exists():
            raise FileNotFoundError(
                f"{SIGNATURE_BLOCK_PATH} not found. This file is the Markdown closing/signature "
                "appended to every drafted email -- create it before running."
            )
        # Strip only the trailing newline(s) read_text() picks up from the
        # file's own end-of-file, not the intentional trailing double-spaces
        # on each line (those are the hard-break markers, not incidental
        # whitespace) -- rstrip("\n") specifically, never a plain rstrip().
        _signature_block_cache = SIGNATURE_BLOCK_PATH.read_text(encoding="utf-8").rstrip("\n")
    return _signature_block_cache

# Domains that show up in "company official website" searches but are never
# the company's own site -- skip these when picking the website URL.
NON_OFFICIAL_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "indeed.com", "glassdoor.com", "zoominfo.com", "bloomberg.com",
    "crunchbase.com", "yelp.com", "bbb.org", "wikipedia.org", "youtube.com",
    "google.com", "maps.google.com", "apollo.io", "pitchbook.com",
    "owler.com", "signalhire.com", "rocketreach.co", "builtin.com",
    "apps.apple.com", "podcasts.apple.com", "play.google.com", "leadiq.com",
    "careers-page.com", "prnewswire.com", "businesswire.com", "globenewswire.com",
}

BASE_SYSTEM_PROMPT = (
    "You are a small US based IT tech company that does full stack "
    "programming and graphic design. You are looking for 1099 contracts "
    "and you offer discounted bill rates"
)


class ContactInfo(BaseModel):
    contact_name: Optional[str] = Field(
        None,
        description="Full name of the target decision-maker described in the "
        "prompt (see decision_maker.md) ONLY if it appears explicitly in the "
        "provided search results. Null if not found.",
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
    category: Literal[*CATEGORIES] = Field(
        description="The single best-fit industry category for this company. "
        "Pick 'Other' only if none of the rest plausibly fit."
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


def make_genai_client() -> genai.Client:
    """The native google-genai client -- used (only) for web-search-grounded
    lookups (find_website, find_contact), since Google Search grounding is
    not available through the OpenAI-compat endpoint make_llm() uses.
    """
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit(
            "GOOGLE_API_KEY in .env is missing or still a placeholder. "
            "Set a real Gemini API key (https://aistudio.google.com/apikey) and try again."
        )
    return genai.Client(api_key=api_key)


def _grounded_structured(client: genai.Client, prompt: str, schema: type[BaseModel], default: BaseModel):
    """Runs a Gemini call with Google Search grounding enabled AND a
    structured (Pydantic) response schema in one shot -- the model searches
    the web, then reports back only the requested fields, `null` for
    anything it couldn't confirm via search. Returns `default` (an empty
    instance of `schema`) on any failure, same fail-soft behavior as the
    Tavily-based version this replaced.
    """
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        return resp.parsed if resp.parsed is not None else default
    except Exception as e:
        print(f"    [grounded search failed: {e}]", file=sys.stderr)
        return default


class WebsiteMatch(BaseModel):
    homepage_domain: Optional[str] = Field(
        None,
        description="The bare domain (e.g. 'acme.com' -- no scheme, no "
        "'www.', no path) of this company's own official homepage, found "
        "via web search. NOT a directory/aggregator listing (LinkedIn, "
        "Crunchbase, LeadIQ, ZoomInfo, Apollo, etc.), a news article, a "
        "press release wire, a job board posting, or any other third-party "
        "page that merely mentions the company. Null if no official site "
        "can be confirmed via search.",
    )


def _normalize_domain(raw: str) -> str:
    raw = raw.strip().lower()
    if "://" in raw:
        raw = urllib.parse.urlparse(raw).netloc or raw
    return raw.removeprefix("www.").split("/")[0]


def find_website(client: genai.Client, company: dict) -> tuple[Optional[str], Optional[str]]:
    """Returns (homepage_url, bare_domain), grounded in a real Gemini web
    search -- never a subpage (only scheme + host is ever returned) and
    never a directory/aggregator site (NON_OFFICIAL_DOMAINS is still checked
    as a defense-in-depth sanity filter on whatever Gemini reports).
    """
    prompt = (
        f"Company: {company['company name']} ({company['city']}, {company['state']})\n"
        f"Description: {company['description']}\n\n"
        "Search the web and identify this company's own official homepage domain."
    )
    result = _grounded_structured(client, prompt, WebsiteMatch, WebsiteMatch())
    if not result.homepage_domain:
        return None, None
    domain = _normalize_domain(result.homepage_domain)
    if not domain:
        return None, None
    is_blocked = any(domain == d or domain.endswith("." + d) for d in NON_OFFICIAL_DOMAINS)
    if is_blocked:
        return None, None
    return f"https://{domain}", domain


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


def find_contact(client: genai.Client, company: dict, domain: Optional[str]) -> ContactInfo:
    """Grounded Gemini web search for the target decision-maker described in
    decision_maker.md (or a generic company email as a fallback). Only
    reports facts it actually finds via search -- never guesses, infers, or
    constructs an email address from a name/domain pattern (that's
    generate_email_permutations' job, done separately in process_company
    once we know this came up empty).
    """
    site_hint = f" (website: {domain})" if domain else ""
    prompt = (
        f"Company: {company['company name']}{site_hint} ({company['city']}, {company['state']})\n\n"
        f"Search the web for {get_decision_maker_description()} at this company, and their "
        "email address, if publicly listed. If no named contact matching that description can "
        "be confirmed, look for a general company contact email (info@, contact@, hello@) "
        "instead. Only report facts you actually find via search -- never guess, infer, or "
        "construct an email address from a name/domain pattern."
    )
    return _grounded_structured(client, prompt, ContactInfo, ContactInfo())


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
        f"{get_overview_blurb()}\n\n"
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
        "Task 2 -- Pick the single best-fit industry category for this company "
        "from the allowed list. Use 'Other' only if none plausibly fit.\n\n"
        "Task 3 -- Write a short, specific cold-outreach email (subject + "
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
        result.body = greeting + "\n\n" + normalize_markdown_linebreaks(result.body) + "\n\n" + get_signature_block()
        return result
    except Exception as e:
        print(f"    [email drafting failed: {e}]", file=sys.stderr)
        return LeadAssessment(ranking=1, category="Other", subject="", body="")


def process_company(client: genai.Client, write_llm: ChatOpenAI, company: dict) -> dict:
    name = company["company name"]
    print(f"  -> {name}")

    website, domain = find_website(client, company)
    contact = find_contact(client, company, domain)

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
        "category": assessment.category,
        "website": website or "",
        "email": email,
        "subject": assessment.subject,
        "body": assessment.body,
    }


def process_unprocessed_leads(
    session: Session,
    client: genai.Client,
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
            row = process_company(client, write_llm, company)
            lead.ranking = row["ranking"]
            lead.category = row["category"]
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

    engine = get_engine(args.db)
    client = make_genai_client()
    write_llm = make_llm(temperature=0.6)

    def report(i, total, lead):
        print(f"[{i}/{total}] {lead.name}")

    with Session(engine) as session:
        remaining_before = session.query(Lead).filter(Lead.processed.is_(False)).count()
        print(f"{remaining_before} unprocessed leads.")
        done = process_unprocessed_leads(
            session, client, write_llm, limit=args.limit, sleep_seconds=args.sleep, on_progress=report
        )
        remaining_after = session.query(Lead).filter(Lead.processed.is_(False)).count()

    print(f"Done. Processed {done} lead(s). {remaining_after} unprocessed remain in {args.db}.")


if __name__ == "__main__":
    main()
