# gen-leads

A personal lead-gen toolkit for Succinct Solutions (full-stack dev / graphic design 1099 contracting). It builds a list of prospect companies, enriches each one (official website, a technical contact, a 1-10 fit ranking, a drafted cold-outreach email), and provides a set of small desktop apps to review and send emails. Everything reads and writes one SQLite database (`leads.db`).

This is a single-user local toolkit — there's no test suite, build step, or packaging; everything runs directly with `python <script>.py`.

## How it works

1. **Import** — LinkedIn company-search results are fetched by a human (or an agent driving a real browser, with a human aware of it) and their page text is handed to `linkedin_import.ingest_linkedin_page_text()`. This deliberately never scrapes LinkedIn automatically (ToS / account-ban risk). New companies are added to the database as unprocessed; `lead_runs_app.py`'s "Copy Scrape Prompt" button generates the exact prompt to paste into a Claude Code session to do this.
2. **Enrich** — `lead_gen.py` (run directly, or via `lead_runs_app.py`'s "Process" button) processes every unprocessed lead: searches the web (Tavily) for the company's official website, searches for and extracts a named technical decision-maker's contact info (falling back to a generic contact address or contact page — nothing is ever fabricated), scores the company 1-10 on fit, and drafts a subject + Markdown-formatted email body (Gemini).
3. **Review & send** — `leads_app.py` lists every lead with edit and "Send Mail" actions. Sending goes out over Gmail SMTP and only updates the database (subject/body edits, `times_contacted`) on a successful send.

## Setup

Requires Python 3.13 and the packages in `requirements.txt`:

```
pip install -r requirements.txt
```

Create a `.env` file in the project root with:

```
GOOGLE_API_KEY=...       # Gemini API key: https://aistudio.google.com/apikey
TAVILY_API_KEY=...       # Free key: https://app.tavily.com
GMAIL_APP_PASSWORD=...   # Gmail App Password (not your account password)
```

To get a Gmail App Password: enable 2-Step Verification on the Google account, then create an App Password for Mail at https://myaccount.google.com/apppasswords.

## Running the apps

```
python main_app.py            # launcher — opens any of the apps below from an Apps menu
python lead_runs_app.py       # log/view scrape coverage; run the enrichment pipeline
python leads_app.py           # browse, edit, disable leads; send drafted emails
python verify_email_app.py    # SMTP-based check for whether a mailbox address exists
python lead_runs_viewer.py    # read-only view of scrape run history
```

CLI scripts:

```
python lead_gen.py --limit 5          # smoke-test the enrichment pipeline on 5 unprocessed leads
python lead_gen.py                    # enrich every unprocessed lead
python regenerate_emails.py           # redraft subject/body for existing leads (e.g. after changing email wording/style)
```

Set `LEADS_GUI_DRY_RUN=1` to make `leads_app.py`'s Send button print the email instead of actually sending — always use this when testing.

## Notes

- `lead_gen.py`'s prompt/style/company blurb (`SUCCINCT_SOLUTIONS_BLURB`, `email_example.md`) is the single source of truth for how drafted emails are worded. After changing it, run `regenerate_emails.py` to backfill existing leads' emails rather than editing `leads.db` bodies directly.
- Every lead is marked `processed=True` after enrichment even if a step failed, so a permanently-broken lead isn't retried (and re-billed) forever.
- This only drafts emails — it never sends anything automatically. Always review contacts and email content before sending; outbound commercial email is subject to CAN-SPAM (truthful subject/headers, clear sender ID, working opt-out, physical address).
