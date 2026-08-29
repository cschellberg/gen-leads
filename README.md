# gen-leads

A personal lead-gen toolkit for Succinct Solutions (full-stack dev / graphic design 1099 contracting). It builds a list of prospect companies, enriches each one (official website, a technical contact, a 1-10 fit ranking, a drafted cold-outreach email), and provides a set of small desktop apps to review and send emails. Everything reads and writes one SQLite database (`leads.db`).

This is a single-user local toolkit — there's no test suite, build step, or packaging; everything runs directly with `python <script>.py`.

## How it works

1. **Import** — LinkedIn company-search results are fetched by a human (or an agent driving a real browser, with a human aware of it) and their page text is handed to `linkedin_import.ingest_linkedin_page_text()`. This deliberately never scrapes LinkedIn automatically (ToS / account-ban risk). New companies are added to the database as unprocessed; `lead_runs_app.py`'s "Copy Scrape Prompt" button generates the exact prompt to paste into a Claude Code session to do this.
2. **Enrich** — `lead_gen.py` (run directly, or via `lead_runs_app.py`'s "Process" button) processes every unprocessed lead: uses Gemini's web-search grounding to find the company's official website and a named contact matching `decision_maker.md`'s description (falling back to email-permutation guesses off the domain, then a generic contact address — nothing is fabricated as a *fact*, only as an explicitly-labeled guess), scores the company 1-10 on fit, tags it with an industry category, and drafts a subject + Markdown-formatted email body.
3. **Review & send** — `leads_app.py` lists every lead (filterable by category, among other filters) with edit and "Send Mail" actions. Sending goes out over Gmail SMTP and only updates the database (subject/body edits, `times_contacted`) on a successful send.

## Setup

Requires Python 3.13 and the packages in `requirements.txt`:

```
pip install -r requirements.txt
```

Create a `.env` file in the project root with:

```
GOOGLE_API_KEY=...       # Gemini API key: https://aistudio.google.com/apikey
GMAIL_APP_PASSWORD=...   # Gmail App Password (not your account password)
```

To get a Gmail App Password: enable 2-Step Verification on the Google account, then create an App Password for Mail at https://myaccount.google.com/apppasswords.

## Customization

To tailor this toolkit to a different business/user, edit these Markdown files in the project root — no code changes needed:

- **`overview.md`** (`OVERVIEW_BLURB`) — describes your company/offering. Fed into both the fit-ranking prompt (deciding whether a prospect is a good fit) and the email-drafting prompt, so it should cover: who you are, your positioning/differentiators, your tech stack or service offering, and your contracting terms (rates, 1099/W2, etc.) — whatever should shape which leads score well and what the email pitches. Write it as normal prose; however it's line-wrapped in the file doesn't matter, it's collapsed into one paragraph before use.
- **`signature_block.md`** — the closing every drafted email ends with (name, title, contact info, website). **Important:** every line except the last must end with two trailing spaces — that's Markdown's "hard break" syntax, and without it the lines collapse into one run-on paragraph when rendered as HTML (see `leads_app.py`'s Send Mail, which renders the Markdown body to HTML before sending). Most editors trim trailing whitespace on save, so double-check it's still there after editing.
- **`decision_maker.md`** — describes who counts as the target contact at a prospect company. Defaults to `a technical decision-maker (CTO, VP of Engineering, Head of Technology, or Director of IT/Engineering)`; if your outreach targets a different function, replace it with something like `a marketing decision-maker (CMO, VP of Marketing, or Director of Marketing)`. Drives what `lead_gen.find_contact()` searches for — write it as a noun phrase that reads naturally after "Search the web for ___ at this company."

All three are loaded once and cached per process — if you edit them while a GUI app or long-running script is already open, restart it to pick up the change. `email_example.md` (Markdown style/structure reference for drafted bodies — tone, section shape, formatting) is also editable but more about *how* things are said than user-specific facts, so it doesn't need to change per user the way the three above do.

After changing any of these, run `python regenerate_emails.py` to redraft existing unsent leads' emails with the new wording rather than leaving them stuck with the old one (`decision_maker.md` changes only affect *future* contact lookups — re-run `lead_gen.py` or `fix_website_and_email.py` to re-search existing leads under the new target).

## Running the apps

```
python main_app.py            # launcher — opens any of the apps below from an Apps menu
python lead_runs_app.py       # log/view scrape coverage; run the enrichment pipeline
python leads_app.py           # browse, edit, disable leads; send drafted emails
python verify_email_app.py    # SMTP-based check for whether a mailbox address exists
```

CLI scripts:

```
python lead_gen.py --limit 5          # smoke-test the enrichment pipeline on 5 unprocessed leads
python lead_gen.py                    # enrich every unprocessed lead
python regenerate_emails.py           # redraft subject/body for existing leads (e.g. after changing email wording/style)
python categorize_leads.py            # backfill `category` for processed leads that predate that column
```

Set `LEADS_GUI_DRY_RUN=1` to make `leads_app.py`'s Send button print the email instead of actually sending — always use this when testing.

## Notes

- `lead_gen.py`'s prompt/style/company files (`overview.md`, `signature_block.md`, `decision_maker.md`, `email_example.md` — see Customization above) are the single source of truth for how drafted emails are worded and who's searched for. After changing any of them, run `regenerate_emails.py` (wording) and/or re-run enrichment (contact target) rather than editing `leads.db` directly.
- Every lead is marked `processed=True` after enrichment even if a step failed, so a permanently-broken lead isn't retried (and re-billed) forever.
- This only drafts emails — it never sends anything automatically. Always review contacts and email content before sending; outbound commercial email is subject to CAN-SPAM (truthful subject/headers, clear sender ID, working opt-out, physical address).
