"""
Database layer for the leads SQLite database. Shared between lead_gen.py and
any other app that reads/writes the same "leads" table -- import from this
module rather than duplicating the model elsewhere.

Usage from another script:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path("path/to/gen-leads")))

    from db import Lead, get_engine, DEFAULT_DB
    from sqlalchemy.orm import Session

    engine = get_engine(DEFAULT_DB)
    with Session(engine) as session:
        for lead in session.query(Lead).filter(Lead.ranking >= 7):
            print(lead.name, lead.email)
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# Loaded here (not left to callers) since DEFAULT_DB below reads DATABASE_NAME
# from the environment at import time -- some scripts import db.py before
# they get around to calling load_dotenv() themselves, which would otherwise
# make DATABASE_NAME silently not take effect. load_dotenv() is a no-op if
# the environment is already populated, so calling it again there is safe.
load_dotenv()

# Resolved relative to this file (not the current working directory), so
# callers get the same database regardless of where they run from. The
# filename is configurable via the DATABASE_NAME env var (with or without a
# ".db" suffix) -- defaults to "leads" (i.e. leads.db) when unset.
SCRIPT_DIR = Path(__file__).resolve().parent
DATABASE_NAME = os.environ.get("DATABASE_NAME", "leads")
if not DATABASE_NAME.endswith(".db"):
    DATABASE_NAME += ".db"
DEFAULT_DB = str(SCRIPT_DIR / DATABASE_NAME)

# The fixed set of industry categories a lead can be tagged with -- shared by
# lead_gen.py (which assigns one during enrichment), the leads_app.py filter
# dropdown, and categorize_leads.py (the one-time backfill for leads that
# predate this column). Keep this list in sync across all three; it's the
# single source of truth.
CATEGORIES = [
    "Advertising & Marketing",
    "Automotive",
    "Construction & Real Estate",
    "Consulting",
    "Education",
    "Entertainment & Media",
    "Finance & Investment",
    "Food & Beverage",
    "Government & Nonprofit",
    "Healthcare & Medicine",
    "Hospitality & Travel",
    "Insurance",
    "Legal",
    "Logistics & Transportation",
    "Manufacturing",
    "Professional Services",
    "Retail & E-commerce",
    "Software & Technology",
    "Sports",
    "Other",
]


class Base(DeclarativeBase):
    pass


class Lead(Base):
    """The one and only table. Each row is one prospect company."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    city: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    ranking: Mapped[int] = mapped_column(Integer, default=1)
    website: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str] = mapped_column(String, default="")
    subject: Mapped[str] = mapped_column(String, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    times_contacted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    processed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class LeadRun(Base):
    """One row per scrape run -- which LinkedIn search URL, and which page
    range (from_page..to_page) was pulled from it -- so it's always clear
    what's already been covered and what hasn't.
    """

    __tablename__ = "lead_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    from_page: Mapped[int] = mapped_column(Integer, nullable=False)
    to_page: Mapped[int] = mapped_column(Integer, nullable=False)
    run_date: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


def _add_column_if_missing(
    engine, column_name: str, ddl_type: str, backfill_true: bool, default_sql: str = "0"
) -> None:
    """Lightweight migration: create_all() only creates missing tables, it
    never alters an existing one, so a column added to the model after the
    database already exists needs to be ALTER TABLE'd in by hand here.

    backfill_true=True sets every row that existed *before* this migration
    ran to True right after adding the column (used for "processed", where
    old rows should count as already processed but the column's own default
    for brand-new rows going forward stays False). default_sql is the
    literal used in "DEFAULT ..." -- "0" for BOOLEAN/INTEGER columns, "''"
    for TEXT ones.
    """
    inspector = inspect(engine)
    if "leads" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("leads")}
    if column_name in columns:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE leads ADD COLUMN {column_name} {ddl_type} DEFAULT {default_sql}"))
        if backfill_true:
            conn.execute(text(f"UPDATE leads SET {column_name} = 1"))


def get_engine(db_path: str = DEFAULT_DB, force: bool = False):
    """Create (if needed) and return a SQLAlchemy engine for the leads DB.

    force=True deletes any existing database file first (used by lead_gen.py's
    --force flag to start over). Other apps reading the data should leave it
    False.
    """
    if force and os.path.exists(db_path):
        os.remove(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    _add_column_if_missing(engine, "processed", "BOOLEAN", backfill_true=True)
    _add_column_if_missing(engine, "category", "TEXT", backfill_true=False, default_sql="''")
    return engine


def get_session(db_path: str = DEFAULT_DB) -> Session:
    """Convenience one-liner for read-only/scripty use: Session(get_engine(...))."""
    return Session(get_engine(db_path))
