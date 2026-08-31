# gen-leads

A personal lead-gen toolkit that builds a list of prospect companies, enriches each one (official website, a decision-maker's contact, a 1-10 fit ranking, a drafted cold-outreach email), and provides a set of small desktop apps to review and send emails. Everything reads and writes one shared SQLite database (`leads.db`).

Multiple businesses/identities can share that one database via **Profiles** (see below) — each lead belongs to a Profile, and is enriched, drafted, and sent under that Profile's own company description, target-contact type, and sender email.

This is a single-user local toolkit — there's no test suite, build step, or packaging; everything runs directly with `python <script>.py`.

## How it works

1. **Import** — LinkedIn company-search results are fetched by a human (or an agent driving a real browser, with a human aware of it) and their page text is handed to `linkedin_import.ingest_linkedin_page_text()`. This deliberately never scrapes LinkedIn automatically (ToS / account-ban risk). New companies are added to the database as unprocessed, tagged with whichever Profile is selected in `main_app.py`'s dropdown at the time; `lead_runs_app.py`'s "Copy Scrape Prompt" button generates the exact prompt (embedding that Profile) to paste into a Claude Code session to do this.
2. **Enrich** — `lead_gen.py` (run directly, or via `lead_runs_app.py`'s "Process" button) processes every unprocessed lead: uses Gemini's web-search grounding to find the company's official website and a named contact matching the lead's Profile's target-decision-maker description (falling back to email-permutation guesses off the domain, then a generic contact address — nothing is fabricated as a *fact*, only as an explicitly-labeled guess), scores the company 1-10 on fit, tags it with an industry category, and drafts a subject + Markdown-formatted email body under that Profile's company description and signature.
3. **Review & send** — `leads_app.py` lists every lead (filterable by category, Profile, and more) with edit and "Send Mail" actions. When run from `main_app.py`, the Profile filter tracks the Active Profile dropdown — switching it there switches which profile's leads are shown here too. Sending goes out over Gmail SMTP *as that lead's own Profile.email* (not necessarily the active one) and only updates the database (subject/body edits, `times_contacted`) on a successful send.

## Setup

Requires Python 3.13. Create a virtual environment and install the packages in `requirements.txt` into it:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Then run any script/app with `.venv\Scripts\python.exe` (or activate the venv first with `.venv\Scripts\Activate.ps1` so plain `python` picks it up).

On a fresh Windows machine with nothing installed yet, `scripts/install.ps1` does all of the above for you (installs Python 3.13 via winget, creates `.venv`, installs `requirements.txt` into it) — right-click it and choose "Run with PowerShell". Safe to re-run any time; once `.venv` exists, re-running only ever touches packages inside it, never the system Python.

Create a `.env` file in the project root with:

```
GOOGLE_API_KEY=...       # Gemini API key: https://aistudio.google.com/apikey
GMAIL_APP_PASSWORD=...   # Gmail App Password (not your account password)
```

To get a Gmail App Password: enable 2-Step Verification on the Google account, then create an App Password for Mail at https://myaccount.google.com/apppasswords. All Profiles currently share this one SMTP credential, so every Profile's email needs to be one this app password can authenticate as (e.g. aliases on the same Gmail account).

First run, seed your first Profile from the original file-based defaults (or skip this and just create one by hand in the Profile app instead):

```
python migrate_to_profile.py
```

## Profiles

A **Profile** (`db.Profile`) is a business/user identity: a sender email plus the three texts that shape enrichment and drafting. Manage them in `main_app.py` → **Manage Profiles**, or standalone with `python profile_app.py`:

- **Email** — the Gmail address leads under this Profile get sent from.
- **Overview** — describes your company/offering. Fed into both the fit-ranking prompt (deciding whether a prospect is a good fit) and the email-drafting prompt, so it should cover: who you are, your positioning/differentiators, your tech stack or service offering, and your contracting terms (rates, 1099/W2, etc.) — whatever should shape which leads score well and what the email pitches.
- **Decision Maker** — describes who counts as the target contact at a prospect company, e.g. `a technical decision-maker (CTO, VP of Engineering, Head of Technology, or Director of IT/Engineering)`. If your outreach targets a different function, write something like `a marketing decision-maker (CMO, VP of Marketing, or Director of Marketing)`. Write it as a noun phrase that reads naturally after "Search the web for ___ at this company."
- **Signature Block** — the closing every drafted email under this Profile ends with (name, title, contact info, website). **Important:** every line except the last must end with two trailing spaces — that's Markdown's "hard break" syntax, and without it the lines collapse into one run-on paragraph when rendered as HTML (see `leads_app.py`'s Send Mail, which renders the Markdown body to HTML before sending). The Profile app's text box doesn't strip trailing spaces, but double-check after pasting from elsewhere.

Every lead belongs to at most one Profile (`Lead.profile_id`). New leads get tagged with whichever Profile is selected in `main_app.py`'s dropdown at scrape time (embedded into the Copy Scrape Prompt text); `lead_gen.resolve_lead_profile()` auto-assigns a lead with no Profile to the sole Profile in the database if there's exactly one, and otherwise raises rather than guessing — assign it explicitly in the Profile app first. `email_example.md` (a Markdown style/structure reference for drafted bodies — tone, section shape, formatting) stays a shared file rather than per-Profile content, since it's about *how* things are said, not user-specific facts.

After editing a Profile's Overview/Decision Maker/Signature Block, existing *unsent* leads under it don't automatically get redrafted — run `python regenerate_emails.py` (wording) and/or re-run enrichment (contact target) if you want existing leads brought up to date rather than just new ones going forward.

## Running the apps

```
python main_app.py            # the app — Apps menu + Profile dropdown; picking a tool embeds it below, no separate windows
```

`main_app.py` is the only window you normally need — its Apps menu swaps which tool is shown in the content area below the Profile dropdown, in place of whatever was there before, rather than opening a new window per tool. Each tool also still runs standalone in its own window, mainly useful for development/debugging:

```
python lead_runs_app.py       # log/view scrape coverage; run the enrichment pipeline
python leads_app.py           # browse, edit, disable leads; send drafted emails
python verify_email_app.py    # SMTP-based check for whether a mailbox address exists
python profile_app.py         # manage Profiles (sender email + overview/decision-maker/signature-block)
```

CLI scripts:

```
python lead_gen.py --limit 5          # smoke-test the enrichment pipeline on 5 unprocessed leads
python lead_gen.py                    # enrich every unprocessed lead
python regenerate_emails.py           # redraft subject/body for existing leads (e.g. after changing a Profile's wording)
python categorize_leads.py            # backfill `category` for processed leads that predate that column
python migrate_to_profile.py          # one-time: seed a Profile from overview.md/decision_maker.md/signature_block.md
```

Set `LEADS_GUI_DRY_RUN=1` to make `leads_app.py`'s Send button print the email instead of actually sending — always use this when testing.

## Notes

- Every lead's enrichment (fit-ranking, contact search, email wording) and send-from address are driven by its own linked Profile (see Profiles above) — not by files or `.env`. After changing a Profile, run `regenerate_emails.py` (wording) and/or re-run enrichment (contact target) rather than editing `leads.db` directly.
- Every lead is marked `processed=True` after enrichment even if a step failed, so a permanently-broken lead isn't retried (and re-billed) forever — the one exception is a setup problem (no Profile resolvable, or a missing `email_example.md`), which stops the whole run instead so it can be fixed and retried.
- This only drafts emails — it never sends anything automatically. Always review contacts and email content before sending; outbound commercial email is subject to CAN-SPAM (truthful subject/headers, clear sender ID, working opt-out, physical address).
