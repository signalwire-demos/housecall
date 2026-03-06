# HouseCall

AI-powered real estate voice agent built on SignalWire SWAIG. Answers inbound calls on behalf of a human real estate agent — handles lead capture, property search, appointment scheduling, and live call transfer.

```
┌─────────────────────────────────────────────────────────────┐
│                     Inbound Call                            │
│                   (SignalWire DID)                          │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                  /swml endpoint                             │
│            (AgentServer · Starlette/ASGI)                   │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│              _per_call_config()                             │
│                                                             │
│   • Trestle reverse-phone lookup (cached 90 days)           │
│   • Returning vs new caller detection                       │
│   • Dynamic greeting customization                          │
│   • global_data initialization                              │
└─────────────────────┬───────────────────────────────────────┘
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
   ┌─────────────┐        ┌─────────────┐
   │ New Caller  │        │  Returning  │
   │             │        │   Caller    │
   │ gather_info │        │             │
   │ → 7 profile │        │ Personalized│
   │   questions │        │  greeting + │
   │ → save_lead │        │  upcoming   │
   │ → score     │        │  appts      │
   └──────┬──────┘        └──────┬──────┘
          │                      │
          └──────────┬───────────┘
                     ▼
┌─────────────────────────────────────────────────────────────┐
│              State Machine (14 SWAIG tools)                 │
│                                                             │
│   Property search · Speed tour · Appointment booking        │
│   Live transfer · Callback scheduling · SMS confirmation    │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│              SQLite (housecall.db · WAL)                    │
│                                                             │
│   leads · properties · appointments · call_log              │
│   call_state · enrichment_cache                             │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                on_summary() · post-call                     │
│                                                             │
│   • Writes calls/<call_id>.json                             │
│   • Deletes ephemeral call_state                            │
│   • Cleans stale states (>24h)                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Conversation State Machine

12 steps in a single `default` context. The agent moves between steps via `route_caller` and tool-driven transitions.

```
                          ┌─────────────┐
                          │   greeting  │
                          └──────┬──────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼                         ▼
          ┌──────────────────┐        ┌─────────────┐
          │ collect_lead     │        │  main_menu  │◄─────────────┐
          │ _profile         │        └──────┬──────┘              │
          │ (gather_info)    │               │                     │
          └────────┬─────────┘    ┌──────────┼──────────┬───────┐  │
                   ▼              ▼          ▼          ▼       ▼  │
          ┌──────────────┐  ┌─────────┐ ┌────────┐ ┌───────┐       │
          │save_lead_step│  │property │ │schedule│ │agent  │       │
          └──────┬───────┘  │_search  │ │_appt   │ │_xfer  │       │
                 │          └────┬────┘ └───┬────┘ └───┬───┘       │
                 │               ▼          │          │           │
                 │         ┌──────────┐     │     ┌────┴─────┐     │
                 │         │speed_tour│     │     │transfer  │     │
                 │         └────┬─────┘     │     │_to_agent │     │
                 │              │           │     │    or    │     │
                 │              ▼           ▼     │schedule  │     │
                 │      ┌──────────────┐          │_callback │     │
                 │      │ schedule     │          └────┬─────┘     │
                 │      │ _viewing     │               │           │
                 │      └──────┬───────┘               │           │
                 │             ▼                       │           │
                 │      ┌──────────────┐               │           │
                 │      │   confirm    │               │           │
                 │      │ _appointment │               │           │
                 │      └──────┬───────┘               │           │
                 │             │                       │           │
                 └─────────────┴────────┬──────────────┘           │
                                        ▼                          │
                                 ┌─────────────┐                   │
                                 │   wrap_up   │                   │
                                 │ (summarize) │                   │
                                 └─────────────┘                   │
                                                                   │
                                 ┌──────────────┐                  │
                                 │error_recovery├──────────────────┘
                                 └──────────────┘
```

### Step Details

| Step | Purpose | Tools | Next Steps |
|------|---------|-------|------------|
| `greeting` | Welcome caller, detect intent | `route_caller` | `collect_lead_profile`, `main_menu` |
| `collect_lead_profile` | gather_info — 7 questions | _(internal)_ | `save_lead_step` |
| `save_lead_step` | Persist lead, compute score | `save_lead` | `main_menu` |
| `main_menu` | Central hub | `route_caller`, `search_properties`, `check_availability`, `modify_appointment`, `cancel_appointment` | `property_search`, `speed_tour`, `schedule_appointment`, `schedule_viewing`, `agent_transfer`, `wrap_up` |
| `property_search` | Find matching listings | `search_properties`, `route_caller` | `speed_tour`, `main_menu`, `wrap_up` |
| `speed_tour` | Present properties 1-by-1 | `present_property`, `next_property`, `schedule_viewing_for_property`, `route_caller` | `schedule_viewing`, `main_menu`, `wrap_up` |
| `schedule_viewing` | Collect date/time for viewing | `check_availability`, `book_appointment`, `decline_appointment` | `confirm_appointment`, `schedule_viewing`, `main_menu` |
| `schedule_appointment` | General consultation/callback | `check_availability`, `book_appointment`, `decline_appointment` | `confirm_appointment`, `main_menu` |
| `confirm_appointment` | Read back details, confirm | `route_caller` | `main_menu`, `wrap_up` |
| `agent_transfer` | Live transfer or callback | `transfer_to_agent`, `schedule_callback`, `route_caller` | `wrap_up`, `main_menu` |
| `wrap_up` | Log summary, say goodbye | `summarize_conversation` | _(terminal)_ |
| `error_recovery` | Handle unexpected states | `route_caller` | `main_menu`, `property_search`, `schedule_appointment`, `agent_transfer`, `wrap_up` |

---

## New Caller Flow

```
caller dials in
       │
       ▼
  greeting: "Thanks for calling! Are you looking
             for properties or want to speak with
             the agent?"
       │
       ▼ route_caller(intent=search_properties)
       │
       ▼ (new caller, no profile)
  collect_lead_profile (gather_info)
       │
       │  1. "What is your first name?"
       │  2. "And your last name?"
       │  3. "What email can we reach you at?" [confirm]
       │  4. "What price range are you looking at?"
       │  5. "What type of property?"
       │  6. "Any preferred neighborhoods?"
       │  7. "What's your timeline for buying?"
       │
       ▼ gather_submit
  save_lead_step
       │
       │  → create/update lead in DB
       │  → compute lead score (0-100)
       │  → auto-qualify if score >= 60
       │
       ▼ (step → property_search)
  search_properties
       │
       ▼ (step → speed_tour)
  present properties one by one...
```

---

## Returning Caller Flow

```
caller dials in (recognized by phone number)
       │
       ▼
  greeting: "Hey Brian, welcome back! I see you
             have a viewing on March 15th at 2 PM.
             Want to search for new properties,
             reschedule, or talk to the agent?"
       │
       │  (collect_lead_profile / save_lead_step
       │   removed from context entirely)
       │
       ▼
  main_menu — full access:
       │
       ├─ search_properties
       ├─ check_availability
       ├─ modify_appointment
       ├─ cancel_appointment
       └─ route_caller → any step
```

---

## Speed Tour Flow

```
search_properties(city="Austin", price_max=600000)
       │
       │  Found 5 properties
       │  call_state.tour_index = 0
       │
       ▼
  "Property 1: A 3 bed, 2 bath single family
   home in Zilker, listed at four hundred
   eighty-five thousand dollars..."
       │
       │  "Schedule a viewing, hear details,
       │   or skip to the next one?"
       │
       ├──── "details" ──→ present_property
       │                      │
       │                      ▼
       │                   full description
       │                   with features
       │
       ├──── "next" ────→ next_property
       │                      │
       │                      ▼
       │                 tour_index++
       │                 "Property 2: ..."
       │
       └──── "schedule" → schedule_viewing_for_property
                               │
                               ▼
                          appointment_draft created
                          step → schedule_viewing
                               │
                               ▼
                          check_availability
                               │
                               ▼
                          book_appointment
                          + SMS confirmation
```

---

## Appointment Booking Flow

```
  "What date and time work for you?"
       │
       │  caller: "next Tuesday at 2pm"
       │
       ▼
  check_availability(date="2026-03-15", time="2pm")
       │
       │  normalize_date → "2026-03-15"
       │  normalize_time → "14:00"
       │
       ├──── no conflict ──→ "That's available. Shall I book it?"
       │                          │
       │                          ▼
       │                     book_appointment
       │                          │
       │                          ├─ create appointment in DB
       │                          ├─ send SMS confirmation
       │                          └─ step → confirm_appointment
       │
       └──── conflict ─────→ "That slot is taken.
                              Could we try a different time?"
                                   │
                                   ▼
                              decline_appointment
                              step → schedule_viewing (retry)
```

---

## SWAIG Tools Reference

| # | Tool | Description | Key Parameters |
|---|------|-------------|----------------|
| 1 | `save_lead` | Save gathered lead profile | _(reads global_data)_ |
| 2 | `route_caller` | Route to step by intent | `intent` (enum) |
| 3 | `search_properties` | Search active listings | `price_min`, `price_max`, `property_type` (enum), `bedrooms_min`, `city`, `neighborhood` |
| 4 | `present_property` | Full property details | `mls_id` (optional) |
| 5 | `next_property` | Advance tour index | _(none)_ |
| 6 | `schedule_viewing_for_property` | Start viewing flow for current property | `mls_id` (optional) |
| 7 | `check_availability` | Check for time slot conflicts | `date`, `time` |
| 8 | `book_appointment` | Create appointment + SMS | `date`, `time`, `appointment_type` (enum), `notes` |
| 9 | `decline_appointment` | Caller wants different time | _(none)_ |
| 10 | `transfer_to_agent` | Live call transfer | _(none)_ |
| 11 | `schedule_callback` | Book a callback | `date`, `time`, `notes` |
| 12 | `modify_appointment` | Reschedule appointment | `appointment_id`, `new_date`, `new_time` |
| 13 | `cancel_appointment` | Cancel appointment | `appointment_id`, `reason` |
| 14 | `summarize_conversation` | Log call summary | `summary`, `outcome` (enum) |

Date/time parameters accept flexible input: `"2pm"`, `"2:30 PM"`, `"14:00"`, `"March 15, 2026"`, `"2026-03-15"` — all normalized internally.

---

## Lead Scoring

| Criterion | Points |
|-----------|--------|
| Email provided | +20 |
| Budget specified | +25 |
| Urgent timeline (ASAP, 1-3 months, etc.) | +25 |
| Property type specified | +15 |
| Preferred locations specified | +15 |
| **Auto-qualify threshold** | **>= 60** |

---

## Database Schema

SQLite with WAL mode (`housecall.db`).

```
┌───────────────┐     ┌──────────────────┐     ┌────────────────┐
│   leads       │     │  appointments    │     │  properties    │
├───────────────┤     ├──────────────────┤     ├────────────────┤
│ id            │◄────│ lead_id          │     │ id             │
│ phone (unique)│     │ lead_phone       │     │ mls_id (unique)│
│ first_name    │     │ lead_name        │     │ address        │
│ last_name     │     │ property_id      │────►│ city / state   │
│ email         │     │ property_address │     │ property_type  │
│ budget_min/max│     │ appointment_type │     │ price          │
│ property_type │     │ appointment_date │     │ beds / baths   │
│ locations     │     │ appointment_time │     │ sqft           │
│ timeline      │     │ duration_minutes │     │ features       │
│ lead_score    │     │ status           │     │ listing_status │
│ lead_status   │     │ notes            │     │ lat / lng      │
│ trestle_*     │     └──────────────────┘     └────────────────┘
└───────────────┘
                      ┌──────────────────┐     ┌────────────────┐
                      │   call_log       │     │  call_state    │
                      ├──────────────────┤     ├────────────────┤
                      │ call_id          │     │ call_id (PK)   │
                      │ caller_phone     │     │ state_json     │
                      │ lead_id          │     │ created_at     │
                      │ call_outcome     │     │ updated_at     │
                      │ summary          │     └────────────────┘
                      │ properties_      │
                      │   discussed      │     ┌────────────────┐
                      └──────────────────┘     │enrichment_cache│
                                               ├────────────────┤
                                               │ phone (PK)     │
                                               │ enrichment_json│
                                               │ enriched_at    │
                                               └────────────────┘
```

---

## Setup

### Requirements

- Python 3.10+
- SignalWire account with a DID

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env with your values
```

Required variables:

| Variable | Description |
|----------|-------------|
| `SIGNALWIRE_PROJECT_ID` | SignalWire project ID |
| `SIGNALWIRE_TOKEN` | SignalWire API token |
| `SIGNALWIRE_SPACE` | SignalWire space name |
| `SIGNALWIRE_PHONE_NUMBER` | Inbound DID / SMS from-number |
| `AGENT_NAME` | Human agent's name |
| `AGENT_PHONE` | Agent's phone for live transfer |
| `MARKET_AREA` | Market area (e.g. "Greater Austin") |

Optional:

| Variable | Default | Description |
|----------|---------|-------------|
| `TRESTLE_API_KEY` | — | Trestle API key for caller enrichment |
| `TRESTLE_ENRICHMENT_ENABLED` | `false` | Enable reverse-phone lookup |
| `GOOGLE_MAPS_API_KEY` | — | Address geocoding |
| `AI_MODEL` | `gpt-oss-120b` | LLM model identifier |
| `AI_TEMPERATURE` | `0.5` | LLM temperature |
| `AI_TOP_P` | `0.5` | LLM top_p |
| `SEED_PROPERTIES` | `true` | Auto-seed 40+ sample listings |
| `PORT` | `3000` | Server port |

### Run

```bash
# Development
source .venv/bin/activate
python trenton.py

# Production
gunicorn trenton:app --bind 0.0.0.0:$PORT --workers 1 --worker-class uvicorn.workers.UvicornWorker
```

### Test

```bash
./test_flow.sh           # Run all 42 SWAIG tool tests
./test_flow.sh --debug   # Verbose output with raw responses
```

---

## Dashboard

Browser-based management UI served at `/` on the same port.

**Tabs:** Leads · Appointments · Call Log · Properties (full CRUD)

**API endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/stats` | Dashboard KPIs |
| GET | `/api/leads` | All leads |
| PUT | `/api/leads/{phone}` | Update lead |
| GET | `/api/appointments` | All appointments |
| GET | `/api/call-log` | Call history |
| GET | `/api/properties` | All listings |
| POST | `/api/properties` | Add listing |
| PUT | `/api/properties/{mls_id}` | Update listing |
| DELETE | `/api/properties/{mls_id}` | Remove listing |

---

## Trestle Enrichment

When enabled, reverse-phone lookup runs silently on every inbound call:

```
caller dials in
       │
       ▼
  check enrichment_cache
       │
       ├── cached + fresh (< 90 days) → use cached
       │
       └── stale or missing
              │
              ▼
         GET trestle /phone?phone=...
              │
              ▼
         extract: owner_name, email, address,
                  line_type, carrier, lat/lng
              │
              ▼
         save to enrichment_cache
              │
              ▼
         stored in global_data._trestle_context
         (NOT surfaced to caller — used for:
           • lat/lng proximity sort in search
           • name/email validation after gather
           • lead enrichment fields in DB)
```

---

## Property Types

Enum values used across search, seed data, and schema:

| Enum Value | Voice Display |
|------------|---------------|
| `single_family` | single family home |
| `condo` | condo |
| `townhouse` | townhouse |
| `multi_family` | multi-family property |
| `land` | land |
| `commercial` | commercial property |
