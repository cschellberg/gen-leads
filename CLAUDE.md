# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal lead-gen toolkit for Succinct Solutions (a solo full-stack dev/design contracting business). It builds a list of prospect companies (currently sourced from LinkedIn company search), enriches each one (website, contact, a 1-10 fit ranking, a drafted cold-outreach email via Gemini), and lets the owner review/send emails through a set of small Tkinter desktop apps. Everything reads/writes one SQLite database (`leads.db`) via a shared SQLAlchemy model in `db.py`.

There is no test suite, build step, linter config, or packaging — this is a single-user local toolkit, run directly with `python <script>.py`.

## Running things

Python interpreter: the project's actual working interpreter (with all of `requirements.txt` installed) is the system install at `C:\Users\dsche\AppData\Local\Programs\Python\Python313\python.exe`, **not** the `.venv` in this repo (that venv exists but has no packages installed — don't assume it's the one to use unless you've verified/installed into it). `python`/`py` are not on PATH in the shell environment this session runs in, so invoke it by full path, e.g.:
```
& "C:\Users\dsche\AppData\Local\Programs\Python\Python313\python.exe" lead_gen.py --limit 5
```

Entry points (each is an independent standalone Tkinter app — see "Architecture" below):
- `python main_app.py` — launcher with an Apps menu that opens the other GUI apps (lazy-imported on first open)
- `python lead_runs_app.py` — log/view scrape coverage, and run the enrichment pipeline ("Process" button) on unprocessed leads
- `python leads_app.py` — browse/edit/disable leads and send drafted emails via Gmail SMTP
- `python verify_email_app.py` — standalone SMTP mailbox-existence probe (GUI wrapper around `verify_email.py`)

CLI/batch scripts (no GUI):
- `python lead_gen.py [--db PATH] [--limit N] [--sleep SECONDS]` — runs the enrichment pipeline over every unprocessed lead. `--limit` is for smoke-testing since it makes real Tavily + Gemini API calls per lead.
- `python regenerate_emails.py [--limit N] [--force] [--sleep SECONDS]` — redrafts subject/body for existing leads via `lead_gen.draft_email()` without repeating the website/contact search or ranking. Use this (not `lead_gen.py`) after changing the email prompt/style/blurb, so existing leads' emails get updated without re-spending on search+ranking. Note: as currently written, its lead-filtering logic is commented out, so it reformats **every** lead in the DB each run, not just badly-formatted ones — check `todo = ...` near the top before assuming `--force` is required to do a full pass.

Safety flags:
- `LEADS_GUI_DRY_RUN=1` (env var) — makes `leads_app.py`'s Send button print the email instead of actually sending over SMTP. The DB is still updated exactly as on a real send. Always use this for testing; never send real mail from a test run.

Required `.env` keys (project root, loaded via `python-dotenv`'s `load_dotenv()`): `GOOGLE_API_KEY` (Gemini, via the OpenAI-compatible endpoint — see below), `TAVILY_API_KEY`, `GMAIL_APP_PASSWORD` (a Gmail App Password, not the account password — see setup instructions in `leads_app.py`'s docstring).

## Architecture

**Single shared table via `db.py`.** `Lead` (table `leads`) and `LeadRun` (table `lead_runs`) are the only two SQLAlchemy models, and every app/script imports them from `db.py` rather than redefining anything. `get_engine()` calls `Base.metadata.create_all()` (creates missing tables only) plus a hand-rolled `_add_column_if_missing()` migration step — since SQLAlchemy's `create_all()` never alters existing tables, any new column added to `Lead` needs a corresponding `ALTER TABLE` added there. `DEFAULT_DB` is resolved relative to `db.py`'s own location, not the CWD, so scripts behave the same regardless of where they're invoked from.

**Idempotent, stage-gated pipeline via `processed`/`disabled` flags on each lead** (no separate job queue or status table):
1. **Import** (`linkedin_import.py`): parses LinkedIn company-search result page text (fetched by a human/agent-driven real browser session — this module never fetches anything itself, deliberately, to avoid LinkedIn ToS/ban risk) and inserts new companies as `processed=False`. Dedup is by a symbol-stripped, case-insensitive company name match. Logs one `LeadRun` row per page range ingested.
2. **Enrich** (`lead_gen.py`'s `process_unprocessed_leads()` / `process_company()`): for each `processed=False` lead — Tavily search for the official website, Tavily search + Gemini structured extraction for a named technical contact (falls back to generic email, then a contact-page URL; never fabricates a contact), Gemini-drafted ranking (1-10) + subject/body. Every lead is marked `processed=True` when done (even on failure, so a permanently-broken lead isn't retried/re-billed forever) — the DB has no separate "failed" state.
3. **Review & send** (`leads_app.py`): browse/filter/edit leads, then send via Gmail SMTP; `times_contacted` increments and the (possibly edited) email/subject/body are persisted only on a *successful* send.

**Email drafting is centralized in `lead_gen.draft_email()`.** `regenerate_emails.py` deliberately imports and reuses it rather than reimplementing drafting, "so the two scripts can never drift out of style with each other" — when changing how emails are worded/styled, change `draft_email()` (and/or `SUCCINCT_SOLUTIONS_BLURB`, `EMAIL_CLOSING`, `email_example.md`) once, and re-run `regenerate_emails.py` to backfill existing rows rather than patching `leads.db` bodies directly (bodies are free-form LLM text, not templated, so find/replace across rows is unreliable — regenerating is the correct way to bulk-update wording). `email_example.md` is a Markdown style/structure reference the drafting prompt is told to follow (tone, section shape, hard-break formatting) — it is not itself sent anywhere.

**Gemini is accessed through `langchain_openai.ChatOpenAI`** pointed at Gemini's OpenAI-compatible endpoint (`GEMINI_BASE_URL` in `lead_gen.py`), not `langchain_google_genai` — `make_llm()` is the one place this is wired up (model name, base URL, API key from `GOOGLE_API_KEY`, temperature). `make_llm()` and `process_unprocessed_leads` are deliberately imported lazily (inside a function, not at module top) by the GUI apps that use them, specifically so opening a Tkinter window never triggers an API-key check or client construction — that only happens once the user actually clicks Process/Verify.

**Every Tkinter app is fully standalone** (own `if __name__ == "__main__": main()`, own window, own DB session) — `main_app.py` is purely a thin launcher that lazy-`importlib.import_module()`s the others as `Toplevel` windows on first use and refocuses an already-open one rather than reopening it. Each GUI app that does slow/blocking work (API calls, SMTP) runs it on a background `threading.Thread` and polls a `queue.Queue` via `root.after(100, ...)` — never blocking the Tk mainloop. All four GUI scripts repeat the same small Windows-specific shim at the top (pointing `TCL_LIBRARY`/`TK_LIBRARY` at `<base>/tcl/tcl8.6` etc. before `tkinter` is imported) to work around a python.org-installer Tcl/Tk path mismatch — keep it if you touch these files, and add it to any new Tkinter entry point.
