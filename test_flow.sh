#!/usr/bin/env bash
# =============================================================================
# Trenton SWAIG Flow Test Harness
#
# Tests all 14 SWAIG functions and the full real estate call flow using
# swaig-test with persistent call state (--call-id).
#
# Usage:  ./test_flow.sh [--debug]
# =============================================================================

SWAIG="swaig-test"
AGENT="trenton.py"
PASS=0
FAIL=0
CALL_ID="test-$(date +%s)"
FUTURE_DATE=$(date -v+7d +%Y-%m-%d)
FUTURE_DATE2=$(date -v+14d +%Y-%m-%d)
DEBUG=false

# Parse flags
for arg in "$@"; do
    case "$arg" in
        --debug|-d) DEBUG=true ;;
    esac
done

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

# ── helpers ──────────────────────────────────────────────────────────────────

check() {
    local label="$1"
    local pattern="$2"
    local byte_len char_len pad
    byte_len=$(printf '%s' "$label" | wc -c)
    char_len=${#label}
    pad=$((57 + byte_len - char_len))
    printf "${CYAN}  %-${pad}s${NC}" "$label"
    if echo "$OUTPUT" | grep -qi "$pattern"; then
        printf "${GREEN}PASS${NC}\n"
        ((PASS++))
    else
        printf "${RED}FAIL${NC} — expected '%s'\n" "$pattern"
        echo "      Got: $(echo "$OUTPUT" | head -2)"
        ((FAIL++))
    fi
}

run() {
    if $DEBUG; then
        printf "\n${DIM}  ▸ swaig-test %s${NC}\n" "$*"
    fi
    OUTPUT=$("$SWAIG" "$AGENT" "$@" 2>/dev/null) || true
    if $DEBUG; then
        echo "$OUTPUT" | while IFS= read -r line; do
            printf "${DIM}    %s${NC}\n" "$line"
        done
    fi
}

section() {
    printf "\n${YELLOW}━━ %s ━━${NC}\n" "$1"
}

# Extract set_global_data JSON from OUTPUT (after Actions: header)
extract_global_data() {
    echo "$OUTPUT" | sed -n '/^Actions:$/,$ p' | sed '1d' | \
        jq -s -c '[.[] | select(has("set_global_data")) | .set_global_data] | add // empty' 2>/dev/null
}

# Wrap GD accumulator as --custom-data JSON
cd_gd() {
    echo "$GD" | jq -c '{global_data: .}'
}

# Run swaig-test with GD as custom-data, then merge any set_global_data into GD
run_and_merge() {
    run --custom-data "$(cd_gd)" "$@"
    local new_data
    new_data=$(extract_global_data)
    if [ -n "$new_data" ]; then
        GD=$(echo "$GD" | jq -c --argjson new "$new_data" '. + $new')
    fi
}

# ── Global data payloads ─────────────────────────────────────────────────────

NEW_CALLER='{"global_data":{"is_new_caller":true,"caller_phone":"+15551234567"}}'

KNOWN_CALLER='{"global_data":{"lead_profile":{"phone":"+15551234567","first_name":"Test","last_name":"User","email":"test@example.com","budget_min":300000,"budget_max":600000,"property_type":"single_family","location":"Austin","timeline":"1-3 months","lead_score":85},"is_new_caller":false,"caller_phone":"+15551234567"}}'

PROFILE_ANSWERS='{"global_data":{"is_new_caller":true,"caller_phone":"+15551234567","lead_answers":{"first_name":"Test","last_name":"User","email":"test@example.com","budget":"300000-600000","property_type":"Single Family","location":"Austin","timeline":"1-3 months"}}}'

# =============================================================================
printf "\n${YELLOW}╔══════════════════════════════════════════════════════════╗${NC}\n"
printf "${YELLOW}║          Trenton SWAIG Flow Test Harness                 ║${NC}\n"
printf "${YELLOW}╚══════════════════════════════════════════════════════════╝${NC}\n"
printf "  Call ID: ${CYAN}%s${NC}\n" "$CALL_ID"

# =============================================================================
section "1. route_caller"
# =============================================================================

# New caller wanting to search → collect_lead_profile
run --raw --call-id "${CALL_ID}-rc-new" --custom-data "$NEW_CALLER" --exec route_caller --intent search_properties
check "New caller + search → collect_lead_profile" "collect_lead_profile"

# Known caller → property_search
run --raw --call-id "${CALL_ID}-rc-known" --custom-data "$KNOWN_CALLER" --exec route_caller --intent search_properties
check "Known caller + search → property_search" "property_search"

# Route to schedule_appointment
run --raw --call-id "${CALL_ID}-rc-sched" --exec route_caller --intent schedule_appointment
check "Intent schedule_appointment → step" "schedule_appointment"

# Route to speak_to_agent
run --raw --call-id "${CALL_ID}-rc-agent" --exec route_caller --intent speak_to_agent
check "Intent speak_to_agent → agent_transfer" "agent_transfer"

# Route to wrap_up
run --raw --call-id "${CALL_ID}-rc-wrap" --exec route_caller --intent wrap_up
check "Intent wrap_up → wrap_up" "wrap_up"

# Route to main_menu
run --raw --call-id "${CALL_ID}-rc-menu" --exec route_caller --intent main_menu
check "Intent main_menu → main_menu" "main_menu"

# =============================================================================
section "2. save_lead"
# =============================================================================

run --raw --call-id "${CALL_ID}-sl" --custom-data "$PROFILE_ANSWERS" --exec save_lead
check "Profile saved" "Profile saved"
check "  → lead score computed" "score"
check "  → change_step: property_search" "property_search"

# =============================================================================
section "3. search_properties"
# =============================================================================

# Search with filters
run --raw --call-id "$CALL_ID" --custom-data "$KNOWN_CALLER" --exec search_properties --property_type single_family --price_max 600000 --city Austin
check "Search single_family Austin ≤600k → results" "Found\|property"
check "  → change_step: speed_tour" "speed_tour"

# Search with no filters → popular listings
run --raw --call-id "${CALL_ID}-sp-all" --exec search_properties
check "No filters → returns results" "Found\|property"

# Search with impossible filters → no results message
run --raw --call-id "${CALL_ID}-sp-none" --exec search_properties --property_type commercial --city Timbuktu
check "No matches → broaden suggestion" "No properties\|broaden"

# Search condos
run --raw --call-id "${CALL_ID}-sp-condo" --exec search_properties --property_type condo
check "Search condos → results" "Found\|property"

# Search townhouses
run --raw --call-id "${CALL_ID}-sp-town" --exec search_properties --property_type townhouse
check "Search townhouses → results" "Found\|property"

# =============================================================================
section "4. present_property"
# =============================================================================

# Present current property (requires prior search on $CALL_ID)
run --raw --call-id "$CALL_ID" --exec present_property
check "Present current property → details" "bedroom\|bathroom\|listed at"

# Present with no search state → error
run --raw --call-id "${CALL_ID}-pp-empty" --exec present_property
check "No property → search first" "No property\|search"

# Present by MLS ID
run --raw --call-id "${CALL_ID}-pp-mls" --exec present_property --mls_id MLS-001
check "Present by MLS ID → details" "Oak Ridge\|bedroom"

# =============================================================================
section "5. next_property"
# =============================================================================

# Advance tour (requires prior search on $CALL_ID)
run --raw --call-id "$CALL_ID" --exec next_property
check "Next property → shows next" "Next property\|Property"

# Exhaust the tour
run --raw --call-id "$CALL_ID" --exec next_property
run --raw --call-id "$CALL_ID" --exec next_property
run --raw --call-id "$CALL_ID" --exec next_property
run --raw --call-id "$CALL_ID" --exec next_property
check "End of tour → main_menu" "last property\|main_menu"

# =============================================================================
section "6. schedule_viewing_for_property"
# =============================================================================

# Re-search so we have a current property
run --raw --call-id "${CALL_ID}-sv" --exec search_properties --city Austin --price_max 500000
run --raw --call-id "${CALL_ID}-sv" --exec schedule_viewing_for_property
check "Schedule viewing → asks for date/time" "date\|time\|when"
check "  → change_step: schedule_viewing" "schedule_viewing"

# No property selected → error
run --raw --call-id "${CALL_ID}-sv-empty" --exec schedule_viewing_for_property
check "No property → search first" "No property\|search"

# =============================================================================
section "7. check_availability"
# =============================================================================

# Check with flexible time input (2pm → 14:00)
run --raw --call-id "${CALL_ID}-ca" --exec check_availability --date "$FUTURE_DATE" --time "2pm"
check "check_availability responds" "available\|booked"
check "  → flexible time normalized" "14:00"

# Check with 24-hour format
run --raw --call-id "${CALL_ID}-ca2" --exec check_availability --date "$FUTURE_DATE2" --time "10:00"
check "24-hour time works" "available\|booked"

# Missing date/time
run --raw --call-id "${CALL_ID}-ca-empty" --exec check_availability --date "" --time ""
check "Missing date/time → error" "provide both"

# =============================================================================
section "8. book_appointment"
# =============================================================================

# Book a viewing
run --raw --call-id "${CALL_ID}-ba" --custom-data "$KNOWN_CALLER" --exec book_appointment --date "$FUTURE_DATE" --time "2pm" --appointment_type viewing
check "Book viewing → confirmed" "booked\|confirmed\|Appointment"
check "  → change_step: confirm_appointment" "confirm_appointment"

# Book a consultation with flexible date
run --raw --call-id "${CALL_ID}-ba2" --custom-data "$KNOWN_CALLER" --exec book_appointment --date "$FUTURE_DATE2" --time "10:30 AM" --appointment_type consultation
check "Book consultation → confirmed" "booked\|confirmed\|Appointment"

# =============================================================================
section "9. decline_appointment"
# =============================================================================

run --raw --call-id "${CALL_ID}-da" --exec decline_appointment
check "Decline → ask for new time" "different\|other\|date\|time"
check "  → change_step: schedule_viewing" "schedule_viewing"

# =============================================================================
section "10. transfer_to_agent"
# =============================================================================

run --raw --call-id "${CALL_ID}-ta" --exec transfer_to_agent
check "Transfer → transfers or offers callback" "transfer\|callback\|connecting"

# =============================================================================
section "11. schedule_callback"
# =============================================================================

# With flexible date/time input
run --raw --call-id "${CALL_ID}-scb" --custom-data "$KNOWN_CALLER" --exec schedule_callback --date "$FUTURE_DATE" --time "3pm" --notes "Wants to discuss financing"
check "Callback scheduled" "Callback scheduled\|scheduled"
check "  → change_step: wrap_up" "wrap_up"

# =============================================================================
section "12. modify_appointment"
# =============================================================================

# First book an appointment to modify
run --raw --call-id "${CALL_ID}-ma" --custom-data "$KNOWN_CALLER" --exec book_appointment --date "$FUTURE_DATE" --time "9:00" --appointment_type viewing

# Extract the appointment ID from the DB (most recent)
APPT_ID=$(python3 -c "
from state_store import get_all_appointments
appts = get_all_appointments()
print(appts[-1]['id'] if appts else '')
" 2>/dev/null)

if [ -n "$APPT_ID" ]; then
    # Modify date
    run --raw --call-id "${CALL_ID}-ma" --custom-data "$KNOWN_CALLER" --exec modify_appointment --appointment_id "$APPT_ID" --new_date "$FUTURE_DATE2"
    check "Modify date → rescheduled" "rescheduled\|Rescheduled"

    # Modify time
    run --raw --call-id "${CALL_ID}-ma" --custom-data "$KNOWN_CALLER" --exec modify_appointment --appointment_id "$APPT_ID" --new_time "4pm"
    check "Modify time → rescheduled or conflict" "rescheduled\|Rescheduled\|booked\|already"

    # Modify non-existent appointment
    run --raw --call-id "${CALL_ID}-ma-bad" --exec modify_appointment --appointment_id 99999
    check "Bad appointment ID → not found" "couldn.t find"
else
    printf "${CYAN}  %-57s${NC}${RED}SKIP${NC} — no appointment to modify\n" "Modify appointment"
    ((FAIL++))
fi

# =============================================================================
section "13. cancel_appointment"
# =============================================================================

if [ -n "$APPT_ID" ]; then
    run --raw --call-id "${CALL_ID}-ca-cancel" --custom-data "$KNOWN_CALLER" --exec cancel_appointment --appointment_id "$APPT_ID" --reason "Changed plans"
    check "Cancel appointment → cancelled" "cancelled\|Cancelled"

    # Cancel non-existent
    run --raw --call-id "${CALL_ID}-ca-bad" --exec cancel_appointment --appointment_id 99999
    check "Bad appointment ID → not found" "couldn.t find"
else
    printf "${CYAN}  %-57s${NC}${RED}SKIP${NC} — no appointment to cancel\n" "Cancel appointment"
    ((FAIL++))
fi

# =============================================================================
section "14. summarize_conversation"
# =============================================================================

run --raw --call-id "$CALL_ID" --custom-data "$KNOWN_CALLER" --exec summarize_conversation --summary "Caller searched for homes in Austin, viewed 5 properties, booked a viewing" --outcome appointment_scheduled
check "Summarize → saved" "saved\|logged\|recorded\|summary"

run --raw --call-id "${CALL_ID}-sum2" --custom-data "$KNOWN_CALLER" --exec summarize_conversation --summary "General inquiry about the Austin market" --outcome general_inquiry
check "General inquiry outcome" "saved\|logged\|recorded\|summary"

# =============================================================================
# Summary
# =============================================================================
printf "\n${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
TOTAL=$((PASS + FAIL))
printf "  Total:  %d\n" "$TOTAL"
printf "  ${GREEN}Passed: %d${NC}\n" "$PASS"
printf "  ${RED}Failed: %d${NC}\n" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
    printf "\n${GREEN}All tests passed!${NC}\n\n"
else
    printf "\n${RED}%d test(s) failed.${NC}\n\n" "$FAIL"
    exit 1
fi
