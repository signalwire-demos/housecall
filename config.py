"""Configuration loader for Trenton real estate voice agent."""

import os
from dotenv import load_dotenv

load_dotenv()

# SignalWire
SIGNALWIRE_PROJECT_ID = os.getenv("SIGNALWIRE_PROJECT_ID", "")
SIGNALWIRE_TOKEN = os.getenv("SIGNALWIRE_TOKEN", "")
SIGNALWIRE_SPACE = os.getenv("SIGNALWIRE_SPACE", "")
SIGNALWIRE_PHONE_NUMBER = os.getenv("SIGNALWIRE_PHONE_NUMBER", "")
DISPLAY_PHONE_NUMBER = os.getenv("DISPLAY_PHONE_NUMBER", "")
SWML_BASIC_AUTH_USER = os.getenv("SWML_BASIC_AUTH_USER", "")
SWML_BASIC_AUTH_PASSWORD = os.getenv("SWML_BASIC_AUTH_PASSWORD", "")
SWML_PROXY_URL_BASE = os.getenv("SWML_PROXY_URL_BASE", "")

# Real Estate Agent
AGENT_NAME = os.getenv("AGENT_NAME", "Your Agent")
AGENT_PHONE = os.getenv("AGENT_PHONE", "")
MARKET_AREA = os.getenv("MARKET_AREA", "Metro Area")

# Trestle Enrichment
TRESTLE_API_KEY = os.getenv("TRESTLE_API_KEY", "")
TRESTLE_BASE_URL = os.getenv("TRESTLE_BASE_URL", "https://api.trestleiq.com/3.2")
TRESTLE_ENRICHMENT_ENABLED = os.getenv("TRESTLE_ENRICHMENT_ENABLED", "false").lower() in ("true", "1", "yes")

# Google Maps
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# Enrichment TTL
TTL_ENRICHMENT_DAYS = int(os.getenv("TTL_ENRICHMENT_DAYS", "90"))

# AI Model
AI_MODEL = os.getenv("AI_MODEL", "gpt-oss-120b")
AI_TOP_P = float(os.getenv("AI_TOP_P", "0.5"))
AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.5"))

# App
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3000"))
MOCK_DELAYS = os.getenv("MOCK_DELAYS", "false").lower() in ("true", "1", "yes")
SEED_PROPERTIES = os.getenv("SEED_PROPERTIES", "true").lower() in ("true", "1", "yes")


def validate():
    """Warn about missing configuration."""
    missing = []
    if not SIGNALWIRE_PHONE_NUMBER:
        missing.append("SIGNALWIRE_PHONE_NUMBER")
    if TRESTLE_ENRICHMENT_ENABLED and not TRESTLE_API_KEY:
        missing.append("TRESTLE_API_KEY (enrichment enabled but no key)")
    if missing:
        print(f"WARNING: Missing config: {', '.join(missing)}")
        print("Some features may not work. Copy .env.example to .env and fill in values.")
