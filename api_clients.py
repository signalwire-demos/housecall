"""API client wrappers for Trestle reverse phone and Google Maps geocoding.

Adapted from Veronica's api_clients.py — only Trestle + geocoding needed.
"""

import logging
import requests

import config

logger = logging.getLogger(__name__)


# ── Trestle Reverse Phone API ───────────────────────────────────────

def _format_address(addr):
    """Format a Trestle address dict into a readable string."""
    if not isinstance(addr, dict):
        return str(addr) if addr else None
    parts = [
        addr.get("street_line_1", ""),
        addr.get("street_line_2", ""),
        addr.get("city", ""),
        addr.get("state_code", ""),
        addr.get("postal_code", ""),
    ]
    return ", ".join(p for p in parts if p) or None


def _parse_emails(emails):
    """Extract email strings from Trestle emails field (string or list)."""
    if isinstance(emails, str) and emails:
        return [emails]
    if isinstance(emails, list):
        result = []
        for e in emails:
            if isinstance(e, dict):
                addr = e.get("email_address") or e.get("address")
                if addr:
                    result.append(addr)
            elif isinstance(e, str) and e:
                result.append(e)
        return result
    return []


def trestle_reverse_phone(phone):
    """Lookup a phone number via Trestle Reverse Phone API.

    Returns a rich dict with all available caller intelligence.
    Returns None on failure.
    """
    if not config.TRESTLE_API_KEY:
        logger.warning("TRESTLE_API_KEY not configured — skipping enrichment")
        return None

    clean_phone = phone.lstrip("+")

    url = f"{config.TRESTLE_BASE_URL}/phone"
    params = {"phone": clean_phone}
    headers = {"x-api-key": config.TRESTLE_API_KEY}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Trestle API error for {phone}: {e}")
        return None

    line_type_raw = (data.get("line_type") or "").lower()

    result = {
        "is_valid": data.get("is_valid"),
        "line_type": line_type_raw,
        "carrier": data.get("carrier"),
        "is_prepaid": data.get("is_prepaid"),
        "is_commercial": data.get("is_commercial"),

        "owner_name": None,
        "firstname": None,
        "lastname": None,
        "candidate_email": None,
        "all_emails": [],
        "candidate_address": None,
        "all_addresses": [],

        "trestle_lat": None,
        "trestle_lng": None,

        "owner_count": 0,
        "confidence_score": None,

        "raw_response": data,
    }

    owners = data.get("owners", [])
    result["owner_count"] = len(owners)

    if not owners:
        return result

    owner = owners[0]
    result["owner_name"] = owner.get("name")
    result["firstname"] = owner.get("firstname")
    result["lastname"] = owner.get("lastname")
    result["confidence_score"] = owner.get("phone_to_name_confidence_score")

    all_emails = _parse_emails(owner.get("emails", []))
    result["all_emails"] = all_emails
    result["candidate_email"] = all_emails[0] if all_emails else None

    addresses = owner.get("current_addresses", [])
    all_addrs = []
    for addr in addresses:
        formatted = _format_address(addr)
        if formatted:
            entry = {"formatted": formatted}
            lat_long = addr.get("lat_long", {}) if isinstance(addr, dict) else {}
            if lat_long:
                entry["lat"] = lat_long.get("latitude")
                entry["lng"] = lat_long.get("longitude")
                entry["accuracy"] = lat_long.get("accuracy")
            all_addrs.append(entry)
    result["all_addresses"] = all_addrs
    if all_addrs:
        result["candidate_address"] = all_addrs[0]["formatted"]
        result["trestle_lat"] = all_addrs[0].get("lat")
        result["trestle_lng"] = all_addrs[0].get("lng")

    return result


# ── Google Maps Geocoding ────────────────────────────────────────────

def geocode_address(address):
    """Geocode an address via Google Maps API. Returns dict or None."""
    if not config.GOOGLE_MAPS_API_KEY:
        logger.warning("GOOGLE_MAPS_API_KEY not configured — skipping geocode")
        return None

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": address,
        "key": config.GOOGLE_MAPS_API_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Google Maps geocode error: {e}")
        return None

    results = data.get("results", [])
    if not results:
        return None

    top = results[0]
    location = top.get("geometry", {}).get("location", {})

    return {
        "formatted_address": top.get("formatted_address"),
        "lat": location.get("lat"),
        "lng": location.get("lng"),
        "confidence": top.get("geometry", {}).get("location_type", ""),
    }
