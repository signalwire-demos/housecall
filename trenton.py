#!/usr/bin/env python3
"""Trenton - AI Real Estate Voice Agent powered by SignalWire.

Manages inbound calls for a human real estate agent: lead capture,
appointment scheduling, property speed tours, and call qualification.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from signalwire_agents import AgentBase, AgentServer
from signalwire_agents.core.function_result import SwaigFunctionResult

import config
from api_clients import trestle_reverse_phone, geocode_address
from mock_property_api import (
    mock_search_properties,
    mock_get_property,
    summarize_property,
    format_price_voice,
)
from state_store import (
    load_call_state, save_call_state, delete_call_state,
    cleanup_stale_states, build_ai_summary,
    get_lead_by_phone, create_lead, update_lead,
    get_all_properties, get_property_by_mls, create_property,
    update_property, delete_property,
    create_appointment, get_all_appointments, check_appointment_conflict,
    get_upcoming_appointments_by_phone, get_appointment_by_id, update_appointment,
    save_call_log, get_all_call_logs,
    get_dashboard_stats, get_all_leads,
    get_enrichment_cache, save_enrichment_cache, enrichment_is_stale,
    seed_properties_if_empty,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, force=True)
logger = logging.getLogger(__name__)

config.validate()


# ── Time Normalization ───────────────────────────────────────────────

def normalize_time(t):
    """Accept '2pm', '2:00 PM', '14:00', etc. and return 'HH:MM' 24-hour."""
    if not t:
        return t
    t = t.strip()
    # Already HH:MM 24-hour
    import re
    m = re.match(r'^(\d{1,2}):(\d{2})$', t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    # 12-hour with optional minutes: "2pm", "2:30 PM", "11:00 am"
    m = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM|a\.m\.|p\.m\.)$', t)
    if m:
        h = int(m.group(1))
        mi = m.group(2) or "00"
        period = m.group(3).lower().replace('.', '')
        if period == "pm" and h != 12:
            h += 12
        elif period == "am" and h == 12:
            h = 0
        return f"{h:02d}:{mi}"
    # Fallback — return as-is
    return t


def normalize_date(d):
    """Accept various date formats and return YYYY-MM-DD."""
    if not d:
        return d
    d = d.strip()
    # Already YYYY-MM-DD
    import re
    if re.match(r'^\d{4}-\d{2}-\d{2}$', d):
        return d
    # Try common formats
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y",
                "%B %d %Y", "%b %d %Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(d, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return d


# ── Lead Scoring ─────────────────────────────────────────────────────

def compute_lead_score(email=None, budget_max=None, timeline=None,
                       property_type=None, preferred_locations=None):
    """Score a lead 0-100. >=60 auto-qualifies."""
    score = 0
    if email:
        score += 20
    if budget_max and budget_max > 0:
        score += 25
    if timeline and timeline.lower() in ("asap", "immediately", "this month",
                                          "next month", "1-3 months", "urgent"):
        score += 25
    if property_type:
        score += 15
    if preferred_locations:
        score += 15
    return min(score, 100)


class TrentonAgent(AgentBase):
    """Trenton - AI Real Estate Voice Agent"""

    def __init__(self):
        super().__init__(
            name="Trenton", route="/swml",
            record_call=True, record_format="wav", record_stereo=True,
        )

        # AI model
        self.set_param("ai_model", config.AI_MODEL)
        self.set_param("enable_text_normalization", "both")
        self.set_prompt_llm_params(top_p=config.AI_TOP_P, temperature=config.AI_TEMPERATURE)

        # Personality
        self.prompt_add_section("Personality",
            f"You are Trenton, a friendly and professional AI assistant for {config.AGENT_NAME}, "
            f"a real estate agent serving the {config.MARKET_AREA} area. "
            "You help callers find properties, schedule viewings, and connect with the agent. "
            "Keep it warm, natural, and helpful — like talking to a knowledgeable friend in real estate."
        )

        # Voice behavior
        self.prompt_add_section("Rules", body="", bullets=[
            "If the caller asks something you don't know, offer to have the agent follow up.",
            "Be proactive about scheduling — if they like a property, suggest a viewing.",
        ])

        # Market context — so the AI knows what's available
        self.prompt_add_section("Market Knowledge",
            f"You serve the {config.MARKET_AREA} area. "
            "Available areas include Austin, Round Rock, Cedar Park, Pflugerville, Leander, "
            "Georgetown, Kyle, Dripping Springs, Liberty Hill, and Wimberley. "
            "Popular neighborhoods: Zilker, South Congress, Tarrytown, Westlake Hills, "
            "Mueller, Barton Creek, East Austin, Downtown, Rainey Street, Brushy Creek, and more. "
            "Property types: Single Family homes, Condos, Townhomes, Multi-Family, and Ranch properties. "
            "Price range spans from around $275,000 to over $2.5 million. "
            "When a caller mentions a preference, use search_properties to find matching listings. "
            "You can search with no filters to show popular listings."
        )

        # Voice
        self.add_language("English", "en-US", "inworld.Alex:inworld-tts-1.5-max")
        self.add_hints(["Trenton", config.AGENT_NAME, config.MARKET_AREA,
                        "MLS", "listing", "open house", "viewing", "showing"])

        # Post-prompt
        self.set_post_prompt("Summarize the conversation.")

        # State machine
        self._define_state_machine()

        # SWAIG tools
        self._define_tools()

        # Per-call dynamic config
        self.set_dynamic_config_callback(self._per_call_config)

    def _define_state_machine(self):
        """Define conversation steps."""
        contexts = self.define_contexts()
        ctx = contexts.add_context("default")

        # GREETING — customized per caller in _per_call_config
        greeting = ctx.add_step("greeting")
        greeting.add_section("Task", "Welcome the caller and determine their needs")
        greeting.set_functions(["route_caller"])
        greeting.set_valid_steps(["collect_lead_profile", "main_menu"])

        # COLLECT LEAD PROFILE — gather_info for new callers
        ctx.add_step("collect_lead_profile") \
            .set_text("Collect the caller's information to help them find the perfect property.") \
            .set_functions("none") \
            .set_gather_info(
                output_key="lead_answers",
                completion_action="next_step",
                prompt=(
                    f"Say: 'Great, let me get a few details so I can help you better.' "
                    "Then collect the caller's profile by asking each question naturally. "
                    "IMPORTANT: Only call gather_submit after the user has explicitly confirmed."
                )
            ) \
            .add_gather_question("first_name", "What is your first name?") \
            .add_gather_question("last_name", "And your last name?") \
            .add_gather_question(
                "email",
                "What email address can we reach you at?",
                confirm=True,
                prompt="Accept the email as spoken. Spell it back for confirmation."
            ) \
            .add_gather_question(
                "budget",
                "What price range are you looking at?",
                prompt="Extract a budget range. Submit as 'MIN-MAX' (e.g., '300000-500000') or just a max (e.g., '500000')."
            ) \
            .add_gather_question(
                "property_type",
                "What type of property are you interested in?",
                prompt="Options: Single Family, Condo, Townhome, Multi-Family, Ranch. Submit the type."
            ) \
            .add_gather_question(
                "location",
                "Any preferred neighborhoods or areas?",
                prompt="Submit the location/neighborhood preference as spoken."
            ) \
            .add_gather_question(
                "timeline",
                "What's your timeline for buying?",
                prompt="Submit as spoken (e.g., 'ASAP', 'next 3 months', '6 months', 'just looking')."
            ) \
            .set_valid_steps(["save_lead_step"])

        # SAVE LEAD — bridge step
        ctx.add_step("save_lead_step") \
            .add_section("Task", "Save the completed lead profile") \
            .add_bullets("Process", [
                "Call save_lead immediately — lead data is in ${lead_answers}",
            ]) \
            .set_functions(["save_lead"]) \
            .set_valid_steps(["main_menu"])

        # MAIN MENU
        main_menu = ctx.add_step("main_menu")
        main_menu.add_section("Task", "Help the caller with their real estate needs")
        main_menu.add_bullets("Process", [
            "If the caller wants to search for properties, call search_properties directly — don't just route, actually search",
            "If they want to schedule a viewing or consultation, call route_caller with intent 'schedule_appointment'",
            "If they want to speak with the agent, call route_caller with intent 'speak_to_agent'",
            "If they want to end the call, call route_caller with intent 'wrap_up'",
            "If they already have search results in ${global_data.call_state}, offer to continue the tour or search again",
            "For a new caller, proactively suggest: 'Would you like me to search for properties in your preferred area?'",
        ])
        main_menu.set_functions(["route_caller", "search_properties", "check_availability", "modify_appointment", "cancel_appointment"])
        main_menu.set_valid_steps(["property_search", "speed_tour", "schedule_appointment", "schedule_viewing", "agent_transfer", "wrap_up"])

        # PROPERTY SEARCH
        property_search = ctx.add_step("property_search")
        property_search.add_section("Task", "Search for properties matching the caller's preferences")
        property_search.add_bullets("Process", [
            "Call search_properties with any known preferences from ${global_data.lead_profile}",
            "Use budget_min/budget_max, property_type, and city/neighborhood from their profile",
            "If no specific preferences, call search_properties with no filters to show popular listings",
            "The search tool will automatically transition to speed_tour with results",
            "If no results found, suggest broadening criteria (different area, higher budget, different type)",
        ])
        property_search.set_functions(["search_properties", "route_caller"])
        property_search.set_valid_steps(["speed_tour", "main_menu", "wrap_up"])

        # SPEED TOUR
        speed_tour = ctx.add_step("speed_tour")
        speed_tour.add_section("Task", "Present properties one by one in a speed tour")
        speed_tour.add_bullets("Process", [
            "The search results are in ${global_data.call_state.search_summaries} — read the current one to the caller",
            "Current tour position is ${global_data.call_state.tour_index}",
            "After reading each property, ask: 'Would you like to schedule a viewing, hear more details, or skip to the next one?'",
            "If they want details, call present_property",
            "If they want next, call next_property",
            "If they want to schedule a viewing, call schedule_viewing_for_property",
            "If they want to go back to the main menu or are done browsing, call route_caller with intent 'main_menu' or 'wrap_up'",
        ])
        speed_tour.set_functions(["present_property", "next_property",
                                  "schedule_viewing_for_property", "route_caller"])
        speed_tour.set_valid_steps(["schedule_viewing", "main_menu", "wrap_up"])

        # SCHEDULE VIEWING
        schedule_viewing = ctx.add_step("schedule_viewing")
        schedule_viewing.add_section("Task", "Collect date and time for a property viewing")
        schedule_viewing.add_bullets("Process", [
            "Ask what date and time work for the caller",
            "Call check_availability to verify no conflicts",
            "If available, proceed to confirm the appointment",
            "If not available, suggest alternative times",
        ])
        schedule_viewing.set_functions(["check_availability", "book_appointment", "decline_appointment"])
        schedule_viewing.set_valid_steps(["confirm_appointment", "schedule_viewing", "main_menu"])

        # SCHEDULE APPOINTMENT (general - consultation/callback)
        schedule_appointment = ctx.add_step("schedule_appointment")
        schedule_appointment.add_section("Task", "Schedule a consultation or callback")
        schedule_appointment.add_bullets("Process", [
            "Ask what type of appointment they'd like (viewing, consultation, callback)",
            "Collect preferred date and time",
            "Call check_availability then book_appointment",
        ])
        schedule_appointment.set_functions(["check_availability", "book_appointment", "decline_appointment"])
        schedule_appointment.set_valid_steps(["confirm_appointment", "main_menu"])

        # CONFIRM APPOINTMENT
        confirm_appointment = ctx.add_step("confirm_appointment")
        confirm_appointment.add_section("Task", "Confirm the appointment details")
        confirm_appointment.add_bullets("Process", [
            "Read back the appointment details: date, time, property (if applicable), and type",
            "Confirm with the caller",
            "Ask if there's anything else they need",
        ])
        confirm_appointment.set_functions(["route_caller"])
        confirm_appointment.set_valid_steps(["main_menu", "wrap_up"])

        # AGENT TRANSFER
        agent_transfer = ctx.add_step("agent_transfer")
        agent_transfer.add_section("Task", "Transfer or schedule callback with the human agent")
        agent_transfer.add_bullets("Process", [
            f"Offer to transfer directly to {config.AGENT_NAME} or schedule a callback",
            "If transfer, call transfer_to_agent",
            "If callback, call schedule_callback",
        ])
        agent_transfer.set_functions(["transfer_to_agent", "schedule_callback", "route_caller"])
        agent_transfer.set_valid_steps(["wrap_up", "main_menu"])

        # WRAP UP
        wrap_up = ctx.add_step("wrap_up")
        wrap_up.add_section("Task", "End the call warmly")
        wrap_up.add_bullets("Process", [
            "Call summarize_conversation with a brief summary of the call",
            f"Thank them for calling and remind them {config.AGENT_NAME} will follow up",
            "Say goodbye warmly",
        ])
        wrap_up.set_functions(["summarize_conversation"])
        wrap_up.set_valid_steps([])

        # ERROR RECOVERY
        error_recovery = ctx.add_step("error_recovery")
        error_recovery.add_section("Task", "Handle unexpected situations gracefully")
        error_recovery.add_bullets("Process", [
            "Apologize briefly for any confusion",
            "Offer to help with property search, scheduling, or connecting with the agent",
            "Route to the appropriate step based on caller's response",
        ])
        error_recovery.set_functions(["route_caller"])
        error_recovery.set_valid_steps(["main_menu", "property_search", "schedule_appointment", "agent_transfer", "wrap_up"])

    @staticmethod
    def _format_appointments_for_ai(appointments):
        """Format appointments into a list the AI can read to the caller."""
        if not appointments:
            return []
        result = []
        for a in appointments:
            # Convert stored time to human-friendly display
            raw_time = a.get("appointment_time", "")
            display_time = raw_time
            try:
                dt = datetime.strptime(raw_time, "%H:%M")
                display_time = dt.strftime("%-I:%M %p")  # e.g. "2:00 PM"
            except (ValueError, TypeError):
                pass
            summary = {
                "id": a["id"],
                "type": a.get("appointment_type", "viewing"),
                "date": a.get("appointment_date", ""),
                "time": display_time,
                "property": a.get("property_address", ""),
                "status": a.get("status", "scheduled"),
            }
            result.append(summary)
        return result

    def _define_tools(self):
        """Define all SWAIG tools."""

        _format_appointments_for_ai = self._format_appointments_for_ai

        def _call_id(raw_data):
            if not isinstance(raw_data, dict):
                return "unknown"
            return raw_data.get("call_id", "unknown")

        def _change_step(result, step):
            logger.info(f"step_change: -> {step}")
            result.swml_change_step(step)

        def _sync_summary(result, state):
            result.update_global_data({"call_state": build_ai_summary(state)})
            return result

        # ─── 1. SAVE LEAD ────────────────────────────────────────────

        @self.tool(
            name="save_lead",
            description="Save the completed lead profile from gathered answers",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Setting up your profile", "Saving your details"]},
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def _save_lead(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            caller_phone = global_data.get("caller_phone", "")
            answers = global_data.get("lead_answers", {})

            first_name = (answers.get("first_name") or "").strip()
            last_name = (answers.get("last_name") or "").strip()
            email = (answers.get("email") or "").strip()

            # Parse budget
            budget_min, budget_max = None, None
            budget_raw = (answers.get("budget") or "").strip()
            if "-" in budget_raw:
                parts = budget_raw.split("-")
                try:
                    budget_min = float(parts[0].strip().replace(",", "").replace("$", ""))
                    budget_max = float(parts[1].strip().replace(",", "").replace("$", ""))
                except (ValueError, IndexError):
                    pass
            elif budget_raw:
                try:
                    budget_max = float(budget_raw.replace(",", "").replace("$", ""))
                except ValueError:
                    pass

            property_type = (answers.get("property_type") or "").strip()
            location = (answers.get("location") or "").strip()
            timeline = (answers.get("timeline") or "").strip()

            preferred_locations = [location] if location else []

            # Compute lead score
            score = compute_lead_score(
                email=email, budget_max=budget_max,
                timeline=timeline, property_type=property_type,
                preferred_locations=preferred_locations,
            )

            # Create/update lead
            lead = create_lead(
                phone=caller_phone,
                first_name=first_name or None,
                last_name=last_name or None,
                email=email or None,
                budget_min=budget_min,
                budget_max=budget_max,
                property_type_preference=property_type or None,
                preferred_locations=preferred_locations or None,
                timeline=timeline or None,
                lead_score=score,
                source="phone",
            )

            # Trestle validation (silent — log only)
            trestle_ctx = global_data.get("_trestle_context")
            if trestle_ctx and lead:
                trestle_name = trestle_ctx.get("owner_name", "")
                trestle_email = trestle_ctx.get("candidate_email", "")
                caller_full = f"{first_name} {last_name}".strip().lower()
                if trestle_name and caller_full:
                    match = trestle_name.lower() in caller_full or caller_full in trestle_name.lower()
                    logger.info(f"Trestle name validation: trestle='{trestle_name}' caller='{caller_full}' match={match}")
                if trestle_email and email:
                    email_match = trestle_email.lower() == email.lower()
                    logger.info(f"Trestle email validation: trestle='{trestle_email}' caller='{email}' match={email_match}")

                # Update lead with Trestle data
                update_lead(caller_phone,
                    trestle_owner_name=trestle_ctx.get("owner_name"),
                    trestle_email=trestle_ctx.get("candidate_email"),
                    trestle_address=trestle_ctx.get("candidate_address"),
                    trestle_line_type=trestle_ctx.get("line_type"),
                    trestle_carrier=trestle_ctx.get("carrier"),
                    trestle_raw=json.dumps(trestle_ctx.get("raw_response", {})),
                    trestle_lat=trestle_ctx.get("trestle_lat"),
                    trestle_lng=trestle_ctx.get("trestle_lng"),
                    last_enriched_at=datetime.utcnow().isoformat(),
                )

            # Auto-qualify if score >= 60
            if score >= 60 and lead:
                update_lead(caller_phone, lead_status="qualified")

            lead_profile = {
                "phone": caller_phone,
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "budget_min": budget_min,
                "budget_max": budget_max,
                "property_type": property_type,
                "location": location,
                "timeline": timeline,
                "lead_score": score,
            }

            result = SwaigFunctionResult(
                f"Profile saved.\n{first_name} {last_name}, score {score}/100.\n"
                "Now search for properties matching their preferences by calling search_properties."
            )
            result.update_global_data({
                "lead_profile": lead_profile,
                "is_new_caller": False,
            })
            _change_step(result, "property_search")
            return result

        # ─── 2. ROUTE CALLER ─────────────────────────────────────────

        @self.tool(
            name="route_caller",
            description="Route the caller to the appropriate step based on their intent",
            parameters={
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "The caller's intent",
                        "enum": ["search_properties", "schedule_viewing",
                                 "schedule_appointment", "speak_to_agent",
                                 "callback", "wrap_up", "main_menu"],
                    },
                },
                "required": ["intent"],
            },
        )
        def _route_caller(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            intent = args.get("intent", "main_menu")
            is_new = global_data.get("is_new_caller", True)
            has_profile = bool(global_data.get("lead_profile"))

            # For new callers wanting to search, collect their info first
            if intent == "search_properties" and is_new and not has_profile:
                result = SwaigFunctionResult(
                    "Let me get a few details first so I can find the best properties for you."
                )
                _change_step(result, "collect_lead_profile")
                return result

            step_map = {
                "search_properties": "property_search",
                "schedule_viewing": "schedule_viewing",
                "schedule_appointment": "schedule_appointment",
                "speak_to_agent": "agent_transfer",
                "callback": "agent_transfer",
                "wrap_up": "wrap_up",
                "main_menu": "main_menu",
            }
            next_step = step_map.get(intent, "main_menu")
            result = SwaigFunctionResult(f"Routing to {next_step}.")
            _change_step(result, next_step)
            return result

        # ─── 3. SEARCH PROPERTIES ─────────────────────────────────────

        @self.tool(
            name="search_properties",
            description="Search available properties based on caller preferences",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Let me search our listings", "Looking through available properties", "Checking what we have"]},
            parameters={
                "type": "object",
                "properties": {
                    "price_min": {"type": "number", "description": "Minimum price"},
                    "price_max": {"type": "number", "description": "Maximum price"},
                    "property_type": {
                        "type": "string",
                        "description": "Property type filter (e.g. single_family, condo, townhouse)",
                        "enum": ["single_family", "condo", "townhouse", "multi_family", "land", "commercial"],
                    },
                    "bedrooms_min": {"type": "integer", "description": "Minimum bedrooms"},
                    "city": {"type": "string", "description": "City to search in"},
                    "neighborhood": {"type": "string", "description": "Neighborhood preference"},
                },
                "required": [],
            },
        )
        def _search_properties(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            # Use caller's saved preferences as defaults
            lead = global_data.get("lead_profile") or {}
            price_min = args.get("price_min") or lead.get("budget_min")
            price_max = args.get("price_max") or lead.get("budget_max")
            property_type = args.get("property_type") or lead.get("property_type")
            bedrooms_min = args.get("bedrooms_min")
            city = args.get("city") or lead.get("location")
            neighborhood = args.get("neighborhood")

            logger.info(f"search_properties: price={price_min}-{price_max} type={property_type} "
                        f"beds={bedrooms_min} city={city} neighborhood={neighborhood}")

            # Trestle proximity hint (silent)
            caller_lat, caller_lng = None, None
            trestle_ctx = global_data.get("_trestle_context")
            if trestle_ctx:
                caller_lat = trestle_ctx.get("trestle_lat")
                caller_lng = trestle_ctx.get("trestle_lng")

            results = mock_search_properties(
                price_min=price_min, price_max=price_max,
                property_type=property_type, bedrooms_min=bedrooms_min,
                city=city, neighborhood=neighborhood,
                max_results=5,
                caller_lat=caller_lat, caller_lng=caller_lng,
            )

            logger.info(f"search_properties: found {len(results)} results")

            if not results:
                return SwaigFunctionResult(
                    "No properties found matching those exact criteria. "
                    "Ask the caller if they'd like to broaden the search — "
                    "try a different area, adjust the price range, or a different property type. "
                    "You can also call search_properties with no filters to show popular listings."
                )

            # Store in call state
            summaries = [summarize_property(p, i + 1) for i, p in enumerate(results)]
            state["search_results"] = results
            state["search_summaries"] = summaries
            state["tour_index"] = 0
            state["current_property"] = results[0] if results else None
            save_call_state(call_id, state)

            # Build response with first property
            response = f"Found {len(results)} properties. Here's the first one:\n\n{summaries[0]}"
            response += "\n\nAsk the caller if they'd like to schedule a viewing, hear more details, or move to the next property."

            result = SwaigFunctionResult(response)
            _sync_summary(result, state)
            _change_step(result, "speed_tour")
            return result

        # ─── 4. PRESENT PROPERTY ──────────────────────────────────────

        @self.tool(
            name="present_property",
            description="Present detailed information about the current property in the tour",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Pulling up that property", "Let me grab the details"]},
            parameters={
                "type": "object",
                "properties": {
                    "mls_id": {"type": "string", "description": "MLS ID of a specific property to present"},
                },
                "required": [],
            },
        )
        def _present_property(args, raw_data):
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            mls_id = args.get("mls_id")
            prop = mock_get_property(mls_id) if mls_id else None
            if not prop:
                prop = state.get("current_property")

            if not prop:
                return SwaigFunctionResult("No property to present. Try searching first.")

            features = prop.get("features", [])
            if isinstance(features, str):
                try:
                    features = json.loads(features)
                except (json.JSONDecodeError, TypeError):
                    features = []

            price_str = format_price_voice(prop.get("price"))
            desc = prop.get("description", "")
            feature_str = ", ".join(features[:5]) if features else "no listed features"

            detail = (
                f"{prop.get('address', 'Address unknown')}, "
                f"{prop.get('city', '')}, {prop.get('state', '')}. "
                f"Listed at {price_str}. "
                f"{prop.get('bedrooms', '?')} bedrooms, {prop.get('bathrooms', '?')} bathrooms, "
                f"{prop.get('sqft', '?')} square feet. "
                f"Built in {prop.get('year_built', 'unknown')}. "
                f"{desc} "
                f"Key features include {feature_str}."
            )

            return SwaigFunctionResult(detail)

        # ─── 5. NEXT PROPERTY ─────────────────────────────────────────

        @self.tool(
            name="next_property",
            description="Move to the next property in the speed tour",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Moving to the next one", "Here's the next property"]},
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def _next_property(args, raw_data):
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            results = state.get("search_results", [])
            summaries = state.get("search_summaries", [])
            idx = state.get("tour_index", 0) + 1

            if idx >= len(results):
                result = SwaigFunctionResult(
                    "That was the last property in our results. "
                    "Would you like to search with different criteria, schedule a viewing for any property we discussed, "
                    "or is there anything else I can help with?"
                )
                _change_step(result, "main_menu")
                return result

            state["tour_index"] = idx
            state["current_property"] = results[idx]
            save_call_state(call_id, state)

            result = SwaigFunctionResult(f"Next property.\n\n{summaries[idx]}")
            _sync_summary(result, state)
            return result

        # ─── 6. SCHEDULE VIEWING FOR PROPERTY ─────────────────────────

        @self.tool(
            name="schedule_viewing_for_property",
            description="Start scheduling a viewing for the current property in the tour",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Let me set that up", "Getting the viewing ready"]},
            parameters={
                "type": "object",
                "properties": {
                    "mls_id": {"type": "string", "description": "MLS ID of the property (optional, defaults to current)"},
                },
                "required": [],
            },
        )
        def _schedule_viewing_for_property(args, raw_data):
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)

            mls_id = args.get("mls_id")
            prop = mock_get_property(mls_id) if mls_id else None
            if not prop:
                prop = state.get("current_property")

            if not prop:
                return SwaigFunctionResult("No property selected. Let's search for properties first.")

            state["appointment_draft"] = {
                "property_id": prop.get("id"),
                "property_mls": prop.get("mls_id"),
                "property_address": prop.get("address", ""),
                "appointment_type": "viewing",
            }
            save_call_state(call_id, state)

            result = SwaigFunctionResult(
                f"Setting up a viewing for {prop.get('address', 'the property')}. "
                "What date and time work best for you?"
            )
            _sync_summary(result, state)
            _change_step(result, "schedule_viewing")
            return result

        # ─── 7. CHECK AVAILABILITY ────────────────────────────────────

        @self.tool(
            name="check_availability",
            description="Check if a date/time slot is available for an appointment",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Let me check the schedule", "Checking availability"]},
            parameters={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Appointment date (e.g. 2026-03-15 or March 15, 2026)"},
                    "time": {"type": "string", "description": "Appointment time (e.g. 2pm, 2:30 PM, or 14:00)"},
                },
                "required": ["date", "time"],
            },
        )
        def _check_availability(args, raw_data):
            date_str = normalize_date(args.get("date", ""))
            time_str = normalize_time(args.get("time", ""))

            if not date_str or not time_str:
                return SwaigFunctionResult("Please provide both a date and time.")

            has_conflict = check_appointment_conflict(date_str, time_str)
            if has_conflict:
                return SwaigFunctionResult(
                    f"That time slot on {date_str} at {time_str} is already booked. "
                    "Could we try a different time?"
                )

            return SwaigFunctionResult(
                f"{date_str} at {time_str} is available. "
                "Shall I go ahead and book it?"
            )

        # ─── 8. BOOK APPOINTMENT ──────────────────────────────────────

        @self.tool(
            name="book_appointment",
            description="Create and confirm an appointment",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Booking that appointment", "Locking in your slot"]},
            parameters={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Appointment date (e.g. 2026-03-15 or March 15, 2026)"},
                    "time": {"type": "string", "description": "Appointment time (e.g. 2pm, 2:30 PM, or 14:00)"},
                    "appointment_type": {
                        "type": "string",
                        "description": "Type of appointment",
                        "enum": ["viewing", "consultation", "callback", "open_house"],
                    },
                    "notes": {"type": "string", "description": "Any additional notes"},
                },
                "required": ["date", "time"],
            },
        )
        def _book_appointment(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            call_id = _call_id(raw_data)
            state = load_call_state(call_id)
            lead = global_data.get("lead_profile", {})
            caller_phone = global_data.get("caller_phone", "")

            date_str = normalize_date(args["date"])
            time_str = normalize_time(args["time"])
            appt_type = args.get("appointment_type", "viewing")
            notes = args.get("notes", "")

            # Get property from draft if viewing
            draft = state.get("appointment_draft") or {}
            property_id = draft.get("property_id")
            property_address = draft.get("property_address", "")

            if appt_type == "viewing" and not property_address:
                current = state.get("current_property")
                if current:
                    property_id = current.get("id")
                    property_address = current.get("address", "")

            # Get lead ID
            lead_record = get_lead_by_phone(caller_phone) if caller_phone else None
            lead_id = lead_record["id"] if lead_record else None
            lead_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()

            appt = create_appointment(
                lead_id=lead_id,
                lead_phone=caller_phone,
                lead_name=lead_name,
                property_id=property_id,
                property_address=property_address,
                appointment_type=appt_type,
                appointment_date=date_str,
                appointment_time=time_str,
                notes=notes,
            )

            # Clear draft
            state["appointment_draft"] = None
            save_call_state(call_id, state)

            # Send SMS confirmation
            sms_body = (
                f"Appointment confirmed!\n"
                f"Type: {appt_type.title()}\n"
                f"Date: {date_str} at {time_str}\n"
            )
            if property_address:
                sms_body += f"Property: {property_address}\n"
            sms_body += f"Agent: {config.AGENT_NAME}\nThank you for choosing HouseCall!"

            result = SwaigFunctionResult(
                f"Appointment booked.\n{appt_type.title()} on {date_str} at {time_str}"
                f"{' at ' + property_address if property_address else ''}. "
                "An SMS confirmation has been sent."
            )

            if caller_phone and config.SIGNALWIRE_PHONE_NUMBER:
                result.send_sms(
                    to_number=caller_phone,
                    from_number=config.SIGNALWIRE_PHONE_NUMBER,
                    body=sms_body,
                )

            _sync_summary(result, state)
            _change_step(result, "confirm_appointment")
            return result

        # ─── 9. DECLINE APPOINTMENT ───────────────────────────────────

        @self.tool(
            name="decline_appointment",
            description="Caller wants a different time, go back to scheduling",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def _decline_appointment(args, raw_data):
            result = SwaigFunctionResult("No problem. What other date or time works for you?")
            _change_step(result, "schedule_viewing")
            return result

        # ─── 10. TRANSFER TO AGENT ────────────────────────────────────

        @self.tool(
            name="transfer_to_agent",
            description="Transfer the call to the human real estate agent",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Connecting you now", "Let me transfer you"]},
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def _transfer_to_agent(args, raw_data):
            if config.AGENT_PHONE:
                result = SwaigFunctionResult(
                    f"Transferring you to {config.AGENT_NAME} now. One moment please."
                )
                result.swml_transfer(dest=config.AGENT_PHONE)
                return result
            else:
                result = SwaigFunctionResult(
                    f"{config.AGENT_NAME} isn't available for a direct transfer right now. "
                    "Would you like me to schedule a callback instead?"
                )
                _change_step(result, "agent_transfer")
                return result

        # ─── 11. SCHEDULE CALLBACK ────────────────────────────────────

        @self.tool(
            name="schedule_callback",
            description="Schedule a callback from the agent",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Scheduling that callback", "Setting that up for you"]},
            parameters={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Callback date (e.g. 2026-03-15 or March 15, 2026)"},
                    "time": {"type": "string", "description": "Callback time (e.g. 2pm, 2:30 PM, or 14:00)"},
                    "notes": {"type": "string", "description": "What they want to discuss"},
                },
                "required": ["date", "time"],
            },
        )
        def _schedule_callback(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            caller_phone = global_data.get("caller_phone", "")
            lead = global_data.get("lead_profile", {})
            lead_record = get_lead_by_phone(caller_phone) if caller_phone else None

            date_str = normalize_date(args["date"])
            time_str = normalize_time(args["time"])

            appt = create_appointment(
                lead_id=lead_record["id"] if lead_record else None,
                lead_phone=caller_phone,
                lead_name=f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip(),
                appointment_type="callback",
                appointment_date=date_str,
                appointment_time=time_str,
                notes=args.get("notes", "Callback requested"),
            )

            result = SwaigFunctionResult(
                f"Callback scheduled for {date_str} at {time_str}. "
                f"{config.AGENT_NAME} will call you back."
            )
            _change_step(result, "wrap_up")
            return result

        # ─── 12. MODIFY APPOINTMENT ───────────────────────────────────

        @self.tool(
            name="modify_appointment",
            description="Reschedule an existing appointment. Provide whichever fields are changing — date, time, or both.",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Updating your appointment", "Let me reschedule that"]},
            parameters={
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer", "description": "The appointment ID to modify"},
                    "new_date": {"type": "string", "description": "New date (e.g. 2026-03-15 or March 15, 2026)"},
                    "new_time": {"type": "string", "description": "New time (e.g. 2pm, 2:00 PM, or 14:00)"},
                },
                "required": ["appointment_id"],
            },
        )
        def _modify_appointment(args, raw_data):
            appt_id = args["appointment_id"]
            new_date = normalize_date(args.get("new_date", ""))
            new_time = normalize_time(args.get("new_time", ""))
            logger.info(f"modify_appointment: id={appt_id!r} type={type(appt_id).__name__}")

            appt = get_appointment_by_id(appt_id)
            logger.info(f"modify_appointment: lookup result={appt!r}")
            if not appt:
                return SwaigFunctionResult("I couldn't find that appointment. Let me check your upcoming appointments again.")

            # Use existing values for whichever field wasn't provided
            final_date = new_date or appt.get("appointment_date", "")
            final_time = new_time or appt.get("appointment_time", "")

            if not final_date or not final_time:
                return SwaigFunctionResult(
                    "I need at least a new date or time to reschedule. "
                    "What date and time would work better for you?"
                )

            has_conflict = check_appointment_conflict(final_date, final_time)
            if has_conflict:
                return SwaigFunctionResult(
                    f"That time slot on {final_date} at {final_time} is already booked. "
                    "Could we try a different time?"
                )

            update_fields = {}
            if new_date:
                update_fields["appointment_date"] = final_date
            if new_time:
                update_fields["appointment_time"] = final_time
            updated = update_appointment(appt_id, **update_fields)

            # Update global_data so the AI sees the change
            global_data = (raw_data or {}).get("global_data", {})
            caller_phone = global_data.get("caller_phone", "")
            if caller_phone:
                upcoming = get_upcoming_appointments_by_phone(caller_phone)
                appt_summaries = _format_appointments_for_ai(upcoming)
                result = SwaigFunctionResult(
                    f"Appointment rescheduled to {final_date} at {final_time}."
                )
                result.update_global_data({"upcoming_appointments": appt_summaries})
            else:
                result = SwaigFunctionResult(
                    f"Appointment rescheduled to {final_date} at {final_time}."
                )
            return result

        # ─── 13. CANCEL APPOINTMENT ───────────────────────────────────

        @self.tool(
            name="cancel_appointment",
            description="Cancel an existing appointment",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Cancelling that appointment"]},
            parameters={
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer", "description": "The appointment ID to cancel"},
                    "reason": {"type": "string", "description": "Optional reason for cancellation"},
                },
                "required": ["appointment_id"],
            },
        )
        def _cancel_appointment(args, raw_data):
            appt_id = args["appointment_id"]
            reason = args.get("reason", "")
            logger.info(f"cancel_appointment: id={appt_id!r} type={type(appt_id).__name__}")

            appt = get_appointment_by_id(appt_id)
            logger.info(f"cancel_appointment: lookup result={appt!r}")
            if not appt:
                return SwaigFunctionResult("I couldn't find that appointment.")

            notes = appt.get("notes", "") or ""
            if reason:
                notes = f"{notes} | Cancelled: {reason}".strip(" |")
            update_appointment(appt_id, status="cancelled", notes=notes)

            # Update global_data
            global_data = (raw_data or {}).get("global_data", {})
            caller_phone = global_data.get("caller_phone", "")
            if caller_phone:
                upcoming = get_upcoming_appointments_by_phone(caller_phone)
                appt_summaries = _format_appointments_for_ai(upcoming)
                result = SwaigFunctionResult("Appointment cancelled.")
                result.update_global_data({"upcoming_appointments": appt_summaries})
            else:
                result = SwaigFunctionResult("Appointment cancelled.")
            return result

        # ─── 14. SUMMARIZE CONVERSATION ───────────────────────────────

        @self.tool(
            name="summarize_conversation",
            description="Generate a call summary when the conversation ends",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Wrapping things up"]},
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Brief summary of the call"},
                    "outcome": {
                        "type": "string",
                        "description": "Call outcome",
                        "enum": ["lead_captured", "appointment_scheduled", "property_tour",
                                 "agent_transfer", "callback_scheduled", "general_inquiry", "hangup"],
                    },
                },
                "required": ["summary"],
            },
        )
        def _summarize_conversation(args, raw_data):
            global_data = (raw_data or {}).get("global_data", {})
            call_id = _call_id(raw_data)
            caller_phone = global_data.get("caller_phone", "")
            lead_record = get_lead_by_phone(caller_phone) if caller_phone else None
            state = load_call_state(call_id)

            # Get properties discussed
            search_results = state.get("search_results", [])
            props_discussed = ", ".join(
                p.get("mls_id", "") for p in (search_results or []) if p
            )

            save_call_log(
                call_id=call_id,
                caller_phone=caller_phone,
                lead_id=lead_record["id"] if lead_record else None,
                call_outcome=args.get("outcome", "general_inquiry"),
                summary=args.get("summary", ""),
                properties_discussed=props_discussed,
            )

            return SwaigFunctionResult("Call summary saved.")

    def _per_call_config(self, query_params, body_params, headers, agent):
        """Pre-call enrichment and returning caller detection."""
        call_data = (body_params or {}).get("call", {})
        caller_phone = call_data.get("from", "")

        # ── Trestle Enrichment (silent) ──────────────────────────────
        trestle_data = None
        if caller_phone and config.TRESTLE_ENRICHMENT_ENABLED:
            cached = get_enrichment_cache(caller_phone)
            if cached and not enrichment_is_stale(cached):
                trestle_data = cached["data"]
                logger.info(f"Trestle cache hit for {caller_phone}")
            else:
                logger.info(f"Trestle lookup for {caller_phone}")
                trestle_data = trestle_reverse_phone(caller_phone)
                if trestle_data:
                    save_enrichment_cache(caller_phone, trestle_data)

        # Store Trestle data in global_data as internal context (NOT in AI prompt)
        trestle_ctx = {}
        if trestle_data:
            trestle_ctx = {
                "owner_name": trestle_data.get("owner_name"),
                "firstname": trestle_data.get("firstname"),
                "lastname": trestle_data.get("lastname"),
                "candidate_email": trestle_data.get("candidate_email"),
                "candidate_address": trestle_data.get("candidate_address"),
                "line_type": trestle_data.get("line_type"),
                "carrier": trestle_data.get("carrier"),
                "trestle_lat": trestle_data.get("trestle_lat"),
                "trestle_lng": trestle_data.get("trestle_lng"),
                "raw_response": trestle_data.get("raw_response", {}),
            }

        # ── Check for returning caller ───────────────────────────────
        lead = get_lead_by_phone(caller_phone) if caller_phone else None

        if lead and lead.get("first_name"):
            # RETURNING CALLER — skip profile, go to main menu
            lead_profile = {
                "phone": caller_phone,
                "first_name": lead.get("first_name", ""),
                "last_name": lead.get("last_name", ""),
                "email": lead.get("email", ""),
                "budget_min": lead.get("budget_min"),
                "budget_max": lead.get("budget_max"),
                "property_type": lead.get("property_type_preference", ""),
                "location": "",
                "timeline": lead.get("timeline", ""),
                "lead_score": lead.get("lead_score", 0),
            }

            # Parse preferred_locations
            locs = lead.get("preferred_locations")
            if isinstance(locs, list) and locs:
                lead_profile["location"] = locs[0]
            elif isinstance(locs, str):
                try:
                    parsed = json.loads(locs)
                    if parsed:
                        lead_profile["location"] = parsed[0] if isinstance(parsed, list) else str(parsed)
                except (json.JSONDecodeError, TypeError):
                    lead_profile["location"] = locs

            # Fetch upcoming appointments for this caller
            upcoming_appts = get_upcoming_appointments_by_phone(caller_phone)
            appt_summaries = self._format_appointments_for_ai(upcoming_appts)

            agent.update_global_data({
                "lead_profile": lead_profile,
                "is_new_caller": False,
                "caller_phone": caller_phone,
                "_trestle_context": trestle_ctx,
                "upcoming_appointments": appt_summaries,
            })

            # Customize greeting for returning caller
            ctx = agent._contexts_builder.get_context("default")
            greeting_step = ctx.get_step("greeting")
            greeting_step.clear_sections()
            greeting_step.add_section("Task", "Welcome back a returning caller and help them immediately")

            greeting_bullets = [
                f"Say: 'Welcome back {lead['first_name']}! Great to hear from you again.'",
            ]
            if appt_summaries:
                greeting_bullets.append(
                    "The caller has upcoming appointments listed in ${global_data.upcoming_appointments}. "
                    "Mention them briefly: 'I see you have a viewing coming up on [date] at [time].' "
                    "Ask if they'd like to keep it, reschedule, or cancel."
                )
                greeting_bullets.append(
                    "If they want to reschedule, ask what new date and/or time they'd prefer, "
                    "then call modify_appointment with the appointment_id and the new_date and/or new_time. "
                    "You can also call check_availability first to verify the slot is open."
                )
                greeting_bullets.append(
                    "If they want to cancel, call cancel_appointment with the appointment_id"
                )
            greeting_bullets.extend([
                "Ask: 'How can I help you today — search for properties, manage your appointments, or speak with the agent?'",
                "If they want properties, call search_properties right away using their profile preferences",
                "Otherwise call route_caller with the appropriate intent",
            ])

            greeting_step.add_bullets("Process", greeting_bullets)
            greeting_step.set_functions(["route_caller", "search_properties", "check_availability", "modify_appointment", "cancel_appointment"])
            greeting_step.set_valid_steps(["main_menu", "property_search", "speed_tour", "schedule_appointment", "schedule_viewing", "agent_transfer"])

            # Remove profile collection steps
            for step_name in ["collect_lead_profile", "save_lead_step"]:
                try:
                    ctx.remove_step(step_name)
                except Exception:
                    pass

            agent.prompt_add_section("Lead Profile", "${global_data.lead_profile}")
            if appt_summaries:
                agent.prompt_add_section("Upcoming Appointments", "${global_data.upcoming_appointments}")

        else:
            # NEW CALLER
            agent.update_global_data({
                "lead_profile": None,
                "is_new_caller": True,
                "caller_phone": caller_phone,
                "_trestle_context": trestle_ctx,
            })

            # Customize greeting for new caller — go straight to profile collection
            ctx = agent._contexts_builder.get_context("default")

            # Reorder so collect_lead_profile comes right after greeting
            ctx.move_step("collect_lead_profile", 1)
            ctx.move_step("save_lead_step", 2)

            greeting_step = ctx.get_step("greeting")
            greeting_step.clear_sections()
            greeting_step.add_section("Task", "Welcome a new caller warmly")
            greeting_step.add_bullets("Process", [
                f"Say: 'Thanks for calling! I'm Trenton, the AI assistant for {config.AGENT_NAME}. I'd love to help you find your perfect property.'",
                "Ask: 'Are you looking to buy a home, or would you like to speak with the agent directly?'",
                "If they want to search, buy, or browse — call route_caller with intent 'search_properties' which will collect their info first",
                "If they want to speak with the agent directly — call route_caller with intent 'speak_to_agent'",
            ])
            greeting_step.set_functions(["route_caller"])
            greeting_step.set_valid_steps(["collect_lead_profile", "agent_transfer"])

    def _render_swml(self, call_id=None, modifications=None):
        """Dump SWML to stderr for debugging."""
        swml = super()._render_swml(call_id, modifications)
        try:
            parsed = json.loads(swml) if isinstance(swml, str) else swml
            print(json.dumps(parsed, indent=2, default=str), file=sys.stderr)
        except Exception:
            print(swml, file=sys.stderr)
        return swml

    def on_summary(self, summary=None, raw_data=None):
        """Called when post-prompt summary is received after call ends."""
        if summary:
            logger.info(f"Call summary: {summary}")

        if raw_data:
            calls_dir = Path(__file__).parent / "calls"
            calls_dir.mkdir(exist_ok=True)
            call_id = raw_data.get("call_id", "unknown")
            out_path = calls_dir / f"{call_id}.json"
            try:
                out_path.write_text(json.dumps(raw_data, indent=2, default=str))
                logger.info(f"Saved call data to {out_path}")
            except Exception as e:
                logger.error(f"Failed to save call data: {e}")

            delete_call_state(call_id)
            cleanup_stale_states(24)


# ── Server Setup ─────────────────────────────────────────────────────

def print_startup_url():
    base = config.SWML_PROXY_URL_BASE
    if base:
        base = base.rstrip("/")
    else:
        host = config.HOST if config.HOST != "0.0.0.0" else "localhost"
        base = f"http://{host}:{config.PORT}"

    user = config.SWML_BASIC_AUTH_USER
    password = config.SWML_BASIC_AUTH_PASSWORD

    if user and password:
        scheme, rest = base.split("://", 1)
        url = f"{scheme}://{user}:{password}@{rest}/swml"
    else:
        url = f"{base}/swml"

    logger.info(f"SWML endpoint: {url}")


def create_server():
    """Create and configure the AgentServer."""
    # Seed properties on startup
    if config.SEED_PROPERTIES:
        seed_properties_if_empty()

    server = AgentServer(host=config.HOST, port=config.PORT)
    server.register(TrentonAgent(), "/swml")

    # ── API Endpoints ────────────────────────────────────────────

    @server.app.get("/api/phone")
    def get_phone():
        return {
            "phone": config.SIGNALWIRE_PHONE_NUMBER,
            "display": config.DISPLAY_PHONE_NUMBER or config.SIGNALWIRE_PHONE_NUMBER,
        }

    @server.app.get("/api/agent-info")
    def get_agent_info():
        return {
            "name": config.AGENT_NAME,
            "market_area": config.MARKET_AREA,
        }

    @server.app.get("/api/stats")
    def get_stats():
        return get_dashboard_stats()

    @server.app.get("/api/leads")
    def api_leads():
        return {"leads": get_all_leads()}

    @server.app.put("/api/leads/{phone:path}")
    async def api_update_lead(phone: str, request):
        body = await request.json()
        lead = update_lead(phone, **body)
        if lead:
            return lead
        return {"error": "Lead not found"}, 404

    @server.app.get("/api/appointments")
    def api_appointments():
        return {"appointments": get_all_appointments()}

    @server.app.get("/api/call-log")
    def api_call_log():
        return {"calls": get_all_call_logs()}

    @server.app.get("/api/properties")
    def api_properties():
        return {"properties": get_all_properties()}

    @server.app.post("/api/properties")
    async def api_create_property(request):
        body = await request.json()
        if not body.get("mls_id") or not body.get("address"):
            return {"error": "mls_id and address are required"}, 400
        prop = create_property(**body)
        return prop

    @server.app.put("/api/properties/{mls_id}")
    async def api_update_property(mls_id: str, request):
        body = await request.json()
        prop = update_property(mls_id, **body)
        if prop:
            return prop
        return {"error": "Property not found"}, 404

    @server.app.delete("/api/properties/{mls_id}")
    def api_delete_property(mls_id: str):
        delete_property(mls_id)
        return {"status": "deleted"}

    # Serve static files from web/ directory
    web_dir = Path(__file__).parent / "web"
    if web_dir.exists():
        server.serve_static_files(str(web_dir))

    print_startup_url()
    return server


server = create_server()
app = server.app

if __name__ == "__main__":
    server.run()
