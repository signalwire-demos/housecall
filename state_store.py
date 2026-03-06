"""SQLite state store for HouseCall real estate voice agent.

6 tables: call_state, leads, properties, appointments, call_log, enrichment_cache.
WAL mode for concurrent ASGI access.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import config

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "housecall.db"

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS call_state (
    call_id    TEXT PRIMARY KEY,
    state_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    phone                    TEXT UNIQUE NOT NULL,
    first_name               TEXT,
    last_name                TEXT,
    email                    TEXT,
    budget_min               REAL,
    budget_max               REAL,
    property_type_preference TEXT,
    preferred_locations      TEXT,
    bedroom_min              INTEGER,
    bathroom_min             INTEGER,
    timeline                 TEXT,
    lead_status              TEXT NOT NULL DEFAULT 'new'
                             CHECK(lead_status IN ('new','qualified','contacted','converted','lost')),
    lead_score               INTEGER DEFAULT 0,
    notes                    TEXT,
    source                   TEXT DEFAULT 'phone',
    trestle_owner_name       TEXT,
    trestle_email            TEXT,
    trestle_address          TEXT,
    trestle_line_type        TEXT,
    trestle_carrier          TEXT,
    trestle_raw              TEXT,
    trestle_lat              REAL,
    trestle_lng              REAL,
    last_enriched_at         TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);

CREATE TABLE IF NOT EXISTS properties (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mls_id          TEXT UNIQUE NOT NULL,
    address         TEXT NOT NULL,
    city            TEXT,
    state           TEXT,
    zip_code        TEXT,
    neighborhood    TEXT,
    property_type   TEXT,
    price           REAL,
    bedrooms        INTEGER,
    bathrooms       REAL,
    sqft            INTEGER,
    lot_size        TEXT,
    year_built      INTEGER,
    description     TEXT,
    features        TEXT,
    listing_status  TEXT NOT NULL DEFAULT 'active'
                    CHECK(listing_status IN ('active','pending','sold','withdrawn')),
    image_url       TEXT,
    virtual_tour_url TEXT,
    lat             REAL,
    lng             REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS appointments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id           INTEGER,
    lead_phone        TEXT,
    lead_name         TEXT,
    property_id       INTEGER,
    property_address  TEXT,
    appointment_type  TEXT NOT NULL DEFAULT 'viewing'
                      CHECK(appointment_type IN ('viewing','consultation','callback','open_house')),
    appointment_date  TEXT NOT NULL,
    appointment_time  TEXT NOT NULL,
    duration_minutes  INTEGER DEFAULT 30,
    status            TEXT NOT NULL DEFAULT 'scheduled'
                      CHECK(status IN ('scheduled','confirmed','completed','cancelled','no_show')),
    notes             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS call_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id             TEXT UNIQUE,
    caller_phone        TEXT,
    lead_id             INTEGER,
    duration_seconds    INTEGER,
    call_outcome        TEXT,
    summary             TEXT,
    properties_discussed TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS enrichment_cache (
    phone           TEXT PRIMARY KEY,
    enrichment_json TEXT NOT NULL,
    enriched_at     TEXT NOT NULL
);
"""


def _connect():
    """Open a new connection with WAL mode."""
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_CREATE_TABLES)
    return conn


# ── Call State ───────────────────────────────────────────────────────

DEFAULT_STATE = {
    "step": "greeting",
    "search_results": None,
    "search_summaries": None,
    "tour_index": 0,
    "current_property": None,
    "appointment_draft": None,
}


def load_call_state(call_id):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT state_json FROM call_state WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row:
            state = json.loads(row[0])
            return {**DEFAULT_STATE, **state}
        return dict(DEFAULT_STATE)
    finally:
        conn.close()


def save_call_state(call_id, state):
    now = time.time()
    blob = json.dumps(state, default=str)
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO call_state (call_id, state_json, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(call_id) DO UPDATE SET
                   state_json = excluded.state_json,
                   updated_at = excluded.updated_at""",
            (call_id, blob, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def delete_call_state(call_id):
    conn = _connect()
    try:
        conn.execute("DELETE FROM call_state WHERE call_id = ?", (call_id,))
        conn.commit()
        logger.info(f"Deleted state for call_id={call_id}")
    finally:
        conn.close()


def cleanup_stale_states(max_age_hours=24):
    cutoff = time.time() - (max_age_hours * 3600)
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM call_state WHERE updated_at < ?", (cutoff,)
        )
        conn.commit()
        if cursor.rowcount:
            logger.info(f"Cleaned up {cursor.rowcount} stale call states")
    finally:
        conn.close()


# ── Leads ────────────────────────────────────────────────────────────

def get_lead_by_phone(phone):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM leads WHERE phone = ?", (phone,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_lead(phone, first_name=None, last_name=None, **kwargs):
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO leads
               (phone, first_name, last_name, email,
                budget_min, budget_max, property_type_preference,
                preferred_locations, bedroom_min, bathroom_min,
                timeline, lead_score, notes, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                   first_name              = COALESCE(excluded.first_name, leads.first_name),
                   last_name               = COALESCE(excluded.last_name, leads.last_name),
                   email                   = COALESCE(excluded.email, leads.email),
                   budget_min              = COALESCE(excluded.budget_min, leads.budget_min),
                   budget_max              = COALESCE(excluded.budget_max, leads.budget_max),
                   property_type_preference = COALESCE(excluded.property_type_preference, leads.property_type_preference),
                   preferred_locations     = COALESCE(excluded.preferred_locations, leads.preferred_locations),
                   bedroom_min             = COALESCE(excluded.bedroom_min, leads.bedroom_min),
                   bathroom_min            = COALESCE(excluded.bathroom_min, leads.bathroom_min),
                   timeline                = COALESCE(excluded.timeline, leads.timeline),
                   lead_score              = COALESCE(excluded.lead_score, leads.lead_score),
                   notes                   = COALESCE(excluded.notes, leads.notes),
                   updated_at              = datetime('now')""",
            (
                phone, first_name, last_name,
                kwargs.get("email"),
                kwargs.get("budget_min"),
                kwargs.get("budget_max"),
                kwargs.get("property_type_preference"),
                json.dumps(kwargs["preferred_locations"]) if kwargs.get("preferred_locations") else None,
                kwargs.get("bedroom_min"),
                kwargs.get("bathroom_min"),
                kwargs.get("timeline"),
                kwargs.get("lead_score", 0),
                kwargs.get("notes"),
                kwargs.get("source", "phone"),
            ),
        )
        conn.commit()
        logger.info(f"Upserted lead phone={phone}")
        return get_lead_by_phone(phone)
    finally:
        conn.close()


def update_lead(phone, **fields):
    allowed = {
        "first_name", "last_name", "email",
        "budget_min", "budget_max", "property_type_preference",
        "preferred_locations", "bedroom_min", "bathroom_min",
        "timeline", "lead_status", "lead_score", "notes", "source",
        "trestle_owner_name", "trestle_email", "trestle_address",
        "trestle_line_type", "trestle_carrier", "trestle_raw",
        "trestle_lat", "trestle_lng", "last_enriched_at",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_lead_by_phone(phone)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [phone]
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE leads SET {set_clause}, updated_at = datetime('now') WHERE phone = ?",
            values,
        )
        conn.commit()
        return get_lead_by_phone(phone)
    finally:
        conn.close()


# ── Enrichment Cache ─────────────────────────────────────────────────

def get_enrichment_cache(phone):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM enrichment_cache WHERE phone = ?", (phone,)
        ).fetchone()
        if row:
            return {
                "phone": row["phone"],
                "data": json.loads(row["enrichment_json"]),
                "enriched_at": row["enriched_at"],
            }
        return None
    finally:
        conn.close()


def save_enrichment_cache(phone, data):
    now = datetime.utcnow().isoformat()
    blob = json.dumps(data, default=str)
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO enrichment_cache (phone, enrichment_json, enriched_at)
               VALUES (?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                   enrichment_json = excluded.enrichment_json,
                   enriched_at = excluded.enriched_at""",
            (phone, blob, now),
        )
        conn.commit()
    finally:
        conn.close()


def enrichment_is_stale(record):
    if not record or not record.get("enriched_at"):
        return True
    try:
        enriched = datetime.fromisoformat(record["enriched_at"])
        return datetime.utcnow() - enriched > timedelta(days=config.TTL_ENRICHMENT_DAYS)
    except (ValueError, TypeError):
        return True


# ── Properties ───────────────────────────────────────────────────────

def get_all_properties(status_filter=None):
    conn = _connect()
    try:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM properties WHERE listing_status = ? ORDER BY created_at DESC",
                (status_filter,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM properties ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_property_by_mls(mls_id):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM properties WHERE mls_id = ?", (mls_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_property_by_id(prop_id):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM properties WHERE id = ?", (prop_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_property(**kwargs):
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO properties
               (mls_id, address, city, state, zip_code, neighborhood,
                property_type, price, bedrooms, bathrooms, sqft, lot_size,
                year_built, description, features, listing_status,
                image_url, virtual_tour_url, lat, lng)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                kwargs["mls_id"], kwargs["address"],
                kwargs.get("city"), kwargs.get("state"), kwargs.get("zip_code"),
                kwargs.get("neighborhood"), kwargs.get("property_type"),
                kwargs.get("price"), kwargs.get("bedrooms"), kwargs.get("bathrooms"),
                kwargs.get("sqft"), kwargs.get("lot_size"), kwargs.get("year_built"),
                kwargs.get("description"), json.dumps(kwargs.get("features", [])),
                kwargs.get("listing_status", "active"),
                kwargs.get("image_url"), kwargs.get("virtual_tour_url"),
                kwargs.get("lat"), kwargs.get("lng"),
            ),
        )
        conn.commit()
        return get_property_by_mls(kwargs["mls_id"])
    finally:
        conn.close()


def update_property(mls_id, **fields):
    allowed = {
        "address", "city", "state", "zip_code", "neighborhood",
        "property_type", "price", "bedrooms", "bathrooms", "sqft",
        "lot_size", "year_built", "description", "features",
        "listing_status", "image_url", "virtual_tour_url", "lat", "lng",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "features" in updates and isinstance(updates["features"], list):
        updates["features"] = json.dumps(updates["features"])
    if not updates:
        return get_property_by_mls(mls_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [mls_id]
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE properties SET {set_clause}, updated_at = datetime('now') WHERE mls_id = ?",
            values,
        )
        conn.commit()
        return get_property_by_mls(mls_id)
    finally:
        conn.close()


def delete_property(mls_id):
    conn = _connect()
    try:
        conn.execute("DELETE FROM properties WHERE mls_id = ?", (mls_id,))
        conn.commit()
    finally:
        conn.close()


# ── Appointments ─────────────────────────────────────────────────────

def create_appointment(**kwargs):
    conn = _connect()
    try:
        cursor = conn.execute(
            """INSERT INTO appointments
               (lead_id, lead_phone, lead_name, property_id, property_address,
                appointment_type, appointment_date, appointment_time,
                duration_minutes, status, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                kwargs.get("lead_id"), kwargs.get("lead_phone"),
                kwargs.get("lead_name"), kwargs.get("property_id"),
                kwargs.get("property_address"), kwargs.get("appointment_type", "viewing"),
                kwargs["appointment_date"], kwargs["appointment_time"],
                kwargs.get("duration_minutes", 30),
                kwargs.get("status", "scheduled"), kwargs.get("notes"),
            ),
        )
        conn.commit()
        appt_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_appointments():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM appointments ORDER BY appointment_date DESC, appointment_time DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def check_appointment_conflict(date, time_str):
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM appointments
               WHERE appointment_date = ? AND appointment_time = ?
               AND status IN ('scheduled', 'confirmed')""",
            (date, time_str),
        ).fetchone()
        return row["cnt"] > 0
    finally:
        conn.close()


def get_upcoming_appointments_by_phone(phone):
    """Get upcoming (not past, not cancelled) appointments for a caller."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE lead_phone = ? AND appointment_date >= ?
               AND status IN ('scheduled', 'confirmed')
               ORDER BY appointment_date ASC, appointment_time ASC""",
            (phone, today),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_appointment_by_id(appt_id):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = ?", (appt_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_appointment(appt_id, **fields):
    """Update an appointment's date, time, status, or notes."""
    allowed = {
        "appointment_date", "appointment_time", "duration_minutes",
        "status", "notes", "appointment_type",
        "property_id", "property_address",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_appointment_by_id(appt_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [appt_id]
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE appointments SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        conn.commit()
        return get_appointment_by_id(appt_id)
    finally:
        conn.close()


# ── Call Log ─────────────────────────────────────────────────────────

def save_call_log(call_id, caller_phone, lead_id=None,
                  duration_seconds=None, call_outcome=None,
                  summary=None, properties_discussed=None):
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO call_log
               (call_id, caller_phone, lead_id, duration_seconds,
                call_outcome, summary, properties_discussed)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(call_id) DO UPDATE SET
                   duration_seconds    = COALESCE(excluded.duration_seconds, call_log.duration_seconds),
                   call_outcome        = COALESCE(excluded.call_outcome, call_log.call_outcome),
                   summary             = COALESCE(excluded.summary, call_log.summary),
                   properties_discussed = COALESCE(excluded.properties_discussed, call_log.properties_discussed)""",
            (call_id, caller_phone, lead_id, duration_seconds,
             call_outcome, summary, properties_discussed),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_call_logs():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM call_log ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Dashboard Stats ──────────────────────────────────────────────────

def get_dashboard_stats():
    conn = _connect()
    try:
        total_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        qualified_leads = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE lead_status = 'qualified' OR lead_score >= 60"
        ).fetchone()[0]
        today = datetime.utcnow().strftime("%Y-%m-%d")
        appointments_today = conn.execute(
            "SELECT COUNT(*) FROM appointments WHERE appointment_date = ? AND status IN ('scheduled','confirmed')",
            (today,),
        ).fetchone()[0]
        calls_today = conn.execute(
            "SELECT COUNT(*) FROM call_log WHERE date(created_at) = ?",
            (today,),
        ).fetchone()[0]
        active_listings = conn.execute(
            "SELECT COUNT(*) FROM properties WHERE listing_status = 'active'"
        ).fetchone()[0]
        return {
            "total_leads": total_leads,
            "qualified_leads": qualified_leads,
            "appointments_today": appointments_today,
            "calls_today": calls_today,
            "active_listings": active_listings,
        }
    finally:
        conn.close()


def get_all_leads():
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM leads ORDER BY created_at DESC").fetchall()
        results = []
        for r in rows:
            d = dict(r)
            if d.get("preferred_locations"):
                try:
                    d["preferred_locations"] = json.loads(d["preferred_locations"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results
    finally:
        conn.close()


# ── AI Summary ───────────────────────────────────────────────────────

def build_ai_summary(state):
    """Lightweight dict for global_data (<1KB). Only what AI needs."""
    summary = {"step": state.get("step", "greeting")}

    if state.get("search_summaries"):
        summary["search_summaries"] = state["search_summaries"]
    if state.get("current_property"):
        summary["current_property"] = state["current_property"]
    summary["tour_index"] = state.get("tour_index", 0)
    summary["has_search_results"] = bool(state.get("search_results"))

    if state.get("appointment_draft"):
        summary["appointment_draft"] = state["appointment_draft"]

    return summary


# ── Seed Properties ──────────────────────────────────────────────────

def seed_properties_if_empty():
    """Insert mock properties on first run if table is empty."""
    conn = _connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
        if count > 0:
            return
    finally:
        conn.close()

    from mock_property_api import SAMPLE_PROPERTIES
    for prop in SAMPLE_PROPERTIES:
        try:
            create_property(**prop)
        except Exception as e:
            logger.warning(f"Failed to seed property {prop.get('mls_id')}: {e}")

    logger.info(f"Seeded {len(SAMPLE_PROPERTIES)} properties into database")
