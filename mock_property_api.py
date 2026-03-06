"""Mock MLS property data for HouseCall.

40+ sample properties with search, voice formatting, and seed data.
"""

import math
import config

# Enum value → voice-friendly display name
PROPERTY_TYPE_DISPLAY = {
    "single_family": "single family home",
    "condo": "condo",
    "townhouse": "townhouse",
    "multi_family": "multi-family property",
    "land": "land",
    "commercial": "commercial property",
}


# ── Voice Formatting Helpers ─────────────────────────────────────────

def format_price_voice(price):
    """Convert price to voice-friendly string. 485000 -> 'four hundred eighty-five thousand dollars'."""
    if not price:
        return "price not available"
    price = int(price)
    if price >= 1_000_000:
        millions = price // 1_000_000
        remainder = (price % 1_000_000) // 1_000
        if remainder:
            return f"{_number_to_words(millions)} million {_number_to_words(remainder)} thousand dollars"
        return f"{_number_to_words(millions)} million dollars"
    elif price >= 1_000:
        thousands = price // 1_000
        remainder = price % 1_000
        if remainder:
            return f"{_number_to_words(thousands)} thousand {_number_to_words(remainder)} dollars"
        return f"{_number_to_words(thousands)} thousand dollars"
    return f"{_number_to_words(price)} dollars"


def format_sqft_voice(sqft):
    """Convert sqft to voice-friendly string. 1850 -> 'eighteen hundred fifty square feet'."""
    if not sqft:
        return "size not available"
    sqft = int(sqft)
    if sqft >= 1000:
        hundreds = sqft // 100
        remainder = sqft % 100
        if remainder:
            return f"{_number_to_words(hundreds)} hundred {_number_to_words(remainder)} square feet"
        return f"{_number_to_words(hundreds)} hundred square feet"
    return f"{_number_to_words(sqft)} square feet"


def _number_to_words(n):
    """Convert integer to English words (0-999)."""
    if n == 0:
        return "zero"
    ones = ["", "one", "two", "three", "four", "five", "six", "seven",
            "eight", "nine", "ten", "eleven", "twelve", "thirteen",
            "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty",
            "sixty", "seventy", "eighty", "ninety"]

    if n < 20:
        return ones[n]
    if n < 100:
        return tens[n // 10] + ("-" + ones[n % 10] if n % 10 else "")
    return ones[n // 100] + " hundred" + (" " + _number_to_words(n % 100) if n % 100 else "")


def summarize_property(prop, index):
    """Create voice-friendly property description for phone reading."""
    price_str = format_price_voice(prop.get("price"))
    sqft_str = format_sqft_voice(prop.get("sqft"))
    beds = prop.get("bedrooms", "unknown")
    baths = prop.get("bathrooms", "unknown")
    ptype = PROPERTY_TYPE_DISPLAY.get(prop.get("property_type", ""), "home")
    city = prop.get("city", "")
    neighborhood = prop.get("neighborhood", "")
    location = neighborhood if neighborhood else city

    summary = f"Property {index}: A {beds} bedroom, {baths} bathroom {ptype}"
    if location:
        summary += f" in {location}"
    summary += f", listed at {price_str}."
    summary += f" It's {sqft_str}"
    if prop.get("year_built"):
        summary += f", built in {prop['year_built']}"
    summary += "."

    if prop.get("description"):
        summary += f" {prop['description']}"

    return summary


# ── Search ───────────────────────────────────────────────────────────

def _haversine(lat1, lng1, lat2, lng2):
    """Distance in miles between two coordinates."""
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _city_match(search_city, prop_city):
    """Flexible city matching — strips state suffixes, checks both directions."""
    if not search_city or not prop_city:
        return True  # no filter = match
    # Normalize: strip state suffix like ", TX" and extra whitespace
    sc = search_city.split(",")[0].strip().lower()
    pc = prop_city.split(",")[0].strip().lower()
    return sc in pc or pc in sc


def _neighborhood_match(search_nb, prop_nb):
    """Flexible neighborhood matching."""
    if not search_nb:
        return True  # no filter = match
    if not prop_nb:
        return True  # don't exclude properties without a neighborhood
    return search_nb.lower() in prop_nb.lower() or prop_nb.lower() in search_nb.lower()


def mock_search_properties(price_min=None, price_max=None, property_type=None,
                           bedrooms_min=None, city=None, neighborhood=None,
                           max_results=5, caller_lat=None, caller_lng=None):
    """Search properties from the database with optional filters.

    Designed to be forgiving — partial matches on city/neighborhood,
    and returns popular listings if no filters match.
    """
    from state_store import get_all_properties
    props = get_all_properties(status_filter="active")

    results = []
    for p in props:
        if price_min and p.get("price") and p["price"] < price_min:
            continue
        if price_max and p.get("price") and p["price"] > price_max:
            continue
        if property_type and p.get("property_type"):
            if property_type.lower() != p["property_type"].lower():
                continue
        if bedrooms_min and p.get("bedrooms") and p["bedrooms"] < bedrooms_min:
            continue
        if not _city_match(city, p.get("city")):
            continue
        if not _neighborhood_match(neighborhood, p.get("neighborhood")):
            continue
        results.append(p)

    # Fallback: if filters were too strict, relax city/neighborhood and try again
    if not results and (city or neighborhood):
        for p in props:
            if price_min and p.get("price") and p["price"] < price_min:
                continue
            if price_max and p.get("price") and p["price"] > price_max:
                continue
            if property_type and p.get("property_type"):
                if property_type.lower() != p["property_type"].lower():
                    continue
            if bedrooms_min and p.get("bedrooms") and p["bedrooms"] < bedrooms_min:
                continue
            results.append(p)

    # Sort by proximity if caller location available
    if caller_lat and caller_lng:
        for r in results:
            if r.get("lat") and r.get("lng"):
                r["_distance"] = _haversine(caller_lat, caller_lng, r["lat"], r["lng"])
            else:
                r["_distance"] = 9999
        results.sort(key=lambda x: x["_distance"])
        for r in results:
            r.pop("_distance", None)
    else:
        results.sort(key=lambda x: x.get("price", 0))

    return results[:max_results]


def mock_get_property(mls_id):
    """Get a single property by MLS ID from the database."""
    from state_store import get_property_by_mls
    return get_property_by_mls(mls_id)


# ── Sample Properties (40+ for seeding) ──────────────────────────────

SAMPLE_PROPERTIES = [
    # --- Single Family Homes ---
    {"mls_id": "MLS-001", "address": "142 Oak Ridge Drive", "city": "Austin", "state": "TX", "zip_code": "78701",
     "neighborhood": "Zilker", "property_type": "single_family", "price": 485000, "bedrooms": 3, "bathrooms": 2.0,
     "sqft": 1850, "lot_size": "0.18 acres", "year_built": 2005,
     "description": "Charming home with updated kitchen and covered patio, walking distance to Barton Springs.",
     "features": ["Updated Kitchen", "Covered Patio", "Hardwood Floors", "2-Car Garage"],
     "listing_status": "active", "lat": 30.2610, "lng": -97.7726},

    {"mls_id": "MLS-002", "address": "789 Maple Court", "city": "Austin", "state": "TX", "zip_code": "78704",
     "neighborhood": "South Congress", "property_type": "single_family", "price": 625000, "bedrooms": 4, "bathrooms": 3.0,
     "sqft": 2400, "lot_size": "0.22 acres", "year_built": 2012,
     "description": "Spacious family home with open floor plan and backyard pool near SoCo shops and restaurants.",
     "features": ["Pool", "Open Floor Plan", "Smart Home", "Walk-in Closets"],
     "listing_status": "active", "lat": 30.2486, "lng": -97.7509},

    {"mls_id": "MLS-003", "address": "321 Sunset Boulevard", "city": "Austin", "state": "TX", "zip_code": "78703",
     "neighborhood": "Tarrytown", "property_type": "single_family", "price": 895000, "bedrooms": 5, "bathrooms": 3.5,
     "sqft": 3200, "lot_size": "0.35 acres", "year_built": 1998,
     "description": "Elegant Tarrytown estate on a tree-lined street with a renovated chef's kitchen.",
     "features": ["Chef's Kitchen", "Wine Cellar", "Home Office", "3-Car Garage", "Mature Trees"],
     "listing_status": "active", "lat": 30.2957, "lng": -97.7748},

    {"mls_id": "MLS-004", "address": "555 Bluebonnet Lane", "city": "Round Rock", "state": "TX", "zip_code": "78664",
     "neighborhood": "Brushy Creek", "property_type": "single_family", "price": 375000, "bedrooms": 3, "bathrooms": 2.0,
     "sqft": 1650, "lot_size": "0.15 acres", "year_built": 2018,
     "description": "Modern home in excellent school district with energy-efficient features.",
     "features": ["Energy Star", "Granite Counters", "Covered Porch", "Community Pool"],
     "listing_status": "active", "lat": 30.5083, "lng": -97.6789},

    {"mls_id": "MLS-005", "address": "87 Hilltop Way", "city": "Austin", "state": "TX", "zip_code": "78746",
     "neighborhood": "Westlake Hills", "property_type": "single_family", "price": 1250000, "bedrooms": 5, "bathrooms": 4.5,
     "sqft": 4100, "lot_size": "0.50 acres", "year_built": 2015,
     "description": "Stunning hilltop residence with panoramic views of the Hill Country and resort-style pool.",
     "features": ["Hill Country Views", "Resort Pool", "Theater Room", "Wine Room", "Smart Home"],
     "listing_status": "active", "lat": 30.2960, "lng": -97.8204},

    {"mls_id": "MLS-006", "address": "234 Pecan Street", "city": "Cedar Park", "state": "TX", "zip_code": "78613",
     "neighborhood": "Twin Creeks", "property_type": "single_family", "price": 420000, "bedrooms": 4, "bathrooms": 2.5,
     "sqft": 2100, "lot_size": "0.20 acres", "year_built": 2020,
     "description": "Like-new construction with modern finishes and spacious backyard in popular community.",
     "features": ["New Construction", "Quartz Counters", "Walk-in Pantry", "Covered Patio"],
     "listing_status": "active", "lat": 30.5217, "lng": -97.8203},

    {"mls_id": "MLS-007", "address": "901 Riverside Drive", "city": "Austin", "state": "TX", "zip_code": "78741",
     "neighborhood": "East Riverside", "property_type": "single_family", "price": 350000, "bedrooms": 2, "bathrooms": 1.5,
     "sqft": 1200, "lot_size": "0.12 acres", "year_built": 1985,
     "description": "Cozy bungalow with original charm near the river and downtown access.",
     "features": ["Original Hardwood", "Fenced Yard", "Detached Garage", "Near Hike/Bike Trail"],
     "listing_status": "active", "lat": 30.2380, "lng": -97.7232},

    {"mls_id": "MLS-008", "address": "456 Heritage Oak Drive", "city": "Georgetown", "state": "TX", "zip_code": "78628",
     "neighborhood": "Sun City", "property_type": "single_family", "price": 325000, "bedrooms": 2, "bathrooms": 2.0,
     "sqft": 1500, "lot_size": "0.10 acres", "year_built": 2016,
     "description": "Beautiful 55-plus community home with low maintenance and resort amenities.",
     "features": ["55+ Community", "Golf Course", "Community Center", "Low Maintenance"],
     "listing_status": "active", "lat": 30.6328, "lng": -97.6781},

    {"mls_id": "MLS-009", "address": "678 Ranch Road", "city": "Dripping Springs", "state": "TX", "zip_code": "78620",
     "neighborhood": "Caliterra", "property_type": "single_family", "price": 575000, "bedrooms": 4, "bathrooms": 3.0,
     "sqft": 2800, "lot_size": "0.45 acres", "year_built": 2021,
     "description": "Hill Country living at its finest with a large lot and outdoor entertaining space.",
     "features": ["Hill Country Views", "Outdoor Kitchen", "3-Car Garage", "Study", "Game Room"],
     "listing_status": "active", "lat": 30.1901, "lng": -98.0867},

    {"mls_id": "MLS-010", "address": "112 Congress Avenue", "city": "Austin", "state": "TX", "zip_code": "78702",
     "neighborhood": "East Austin", "property_type": "single_family", "price": 550000, "bedrooms": 3, "bathrooms": 2.0,
     "sqft": 1700, "lot_size": "0.14 acres", "year_built": 2019,
     "description": "Modern East Austin home with an ADU perfect for rental income or guests.",
     "features": ["ADU/Guest House", "Modern Design", "Private Yard", "Near Downtown"],
     "listing_status": "active", "lat": 30.2632, "lng": -97.7284},

    # --- Condos/Townhomes ---
    {"mls_id": "MLS-011", "address": "200 Congress Ave Unit 12A", "city": "Austin", "state": "TX", "zip_code": "78701",
     "neighborhood": "Downtown", "property_type": "condo", "price": 450000, "bedrooms": 2, "bathrooms": 2.0,
     "sqft": 1100, "year_built": 2018,
     "description": "High-rise downtown condo with skyline views and full-service amenities.",
     "features": ["Skyline Views", "Concierge", "Rooftop Pool", "Fitness Center", "Parking Garage"],
     "listing_status": "active", "lat": 30.2672, "lng": -97.7431},

    {"mls_id": "MLS-012", "address": "350 Trinity Street Unit 5B", "city": "Austin", "state": "TX", "zip_code": "78701",
     "neighborhood": "Rainey Street", "property_type": "condo", "price": 525000, "bedrooms": 2, "bathrooms": 2.0,
     "sqft": 1250, "year_built": 2020,
     "description": "Luxury condo in the Rainey Street district with floor-to-ceiling windows.",
     "features": ["Floor-to-Ceiling Windows", "Balcony", "Dog Park", "Valet Parking"],
     "listing_status": "active", "lat": 30.2595, "lng": -97.7393},

    {"mls_id": "MLS-013", "address": "700 Lavaca Street Unit 8C", "city": "Austin", "state": "TX", "zip_code": "78701",
     "neighborhood": "Downtown", "property_type": "condo", "price": 385000, "bedrooms": 1, "bathrooms": 1.0,
     "sqft": 800, "year_built": 2016,
     "description": "Efficient one-bedroom with great walkability score and modern finishes.",
     "features": ["Walk Score 95", "In-Unit Laundry", "Bike Storage", "Pool"],
     "listing_status": "active", "lat": 30.2686, "lng": -97.7487},

    {"mls_id": "MLS-014", "address": "1500 South Lamar Unit 22", "city": "Austin", "state": "TX", "zip_code": "78704",
     "neighborhood": "South Lamar", "property_type": "condo", "price": 335000, "bedrooms": 1, "bathrooms": 1.0,
     "sqft": 750, "year_built": 2014,
     "description": "Hip South Lamar condo near dining and entertainment with assigned parking.",
     "features": ["Assigned Parking", "Updated Appliances", "Community Pool", "Pet Friendly"],
     "listing_status": "active", "lat": 30.2487, "lng": -97.7695},

    {"mls_id": "MLS-015", "address": "88 Red River Lofts", "city": "Austin", "state": "TX", "zip_code": "78701",
     "neighborhood": "Red River", "property_type": "condo", "price": 410000, "bedrooms": 2, "bathrooms": 1.0,
     "sqft": 950, "year_built": 2010,
     "description": "Industrial loft conversion with exposed brick and soaring 14-foot ceilings.",
     "features": ["Exposed Brick", "14ft Ceilings", "Industrial Design", "Rooftop Terrace"],
     "listing_status": "active", "lat": 30.2686, "lng": -97.7359},

    {"mls_id": "MLS-016", "address": "2200 Barton Creek Blvd Unit 3", "city": "Austin", "state": "TX", "zip_code": "78735",
     "neighborhood": "Barton Creek", "property_type": "townhouse", "price": 595000, "bedrooms": 3, "bathrooms": 2.5,
     "sqft": 1900, "year_built": 2019,
     "description": "Upscale townhome backing to the greenbelt with high-end finishes throughout.",
     "features": ["Greenbelt Access", "Quartz Counters", "Private Garage", "Community Pool"],
     "listing_status": "active", "lat": 30.2565, "lng": -97.8161},

    {"mls_id": "MLS-017", "address": "450 Mueller Boulevard Unit 7", "city": "Austin", "state": "TX", "zip_code": "78723",
     "neighborhood": "Mueller", "property_type": "townhouse", "price": 475000, "bedrooms": 3, "bathrooms": 2.5,
     "sqft": 1650, "year_built": 2022,
     "description": "New Mueller townhome in walkable community near parks and Lake Park.",
     "features": ["Walkable Community", "Near Lake Park", "Rooftop Deck", "EV Charger Ready"],
     "listing_status": "active", "lat": 30.2991, "lng": -97.7053},

    # --- Luxury ---
    {"mls_id": "MLS-018", "address": "1 Barton Creek Estate", "city": "Austin", "state": "TX", "zip_code": "78735",
     "neighborhood": "Barton Creek", "property_type": "single_family", "price": 2500000, "bedrooms": 6, "bathrooms": 5.5,
     "sqft": 6200, "lot_size": "1.2 acres", "year_built": 2020,
     "description": "Magnificent Barton Creek estate with infinity pool and panoramic golf course views.",
     "features": ["Infinity Pool", "Golf Course Views", "Wine Cellar", "Theater", "Guest Suite", "Smart Home"],
     "listing_status": "active", "lat": 30.2718, "lng": -97.8352},

    {"mls_id": "MLS-019", "address": "55 Mount Bonnell Road", "city": "Austin", "state": "TX", "zip_code": "78731",
     "neighborhood": "Mount Bonnell", "property_type": "single_family", "price": 1850000, "bedrooms": 4, "bathrooms": 4.0,
     "sqft": 3800, "lot_size": "0.75 acres", "year_built": 2017,
     "description": "Architectural masterpiece near Mount Bonnell with Lake Austin views.",
     "features": ["Lake Views", "Infinity Pool", "Modern Architecture", "Home Gym", "Elevator"],
     "listing_status": "active", "lat": 30.3225, "lng": -97.7732},

    {"mls_id": "MLS-020", "address": "900 Bee Cave Road", "city": "Austin", "state": "TX", "zip_code": "78746",
     "neighborhood": "Rob Roy", "property_type": "single_family", "price": 1650000, "bedrooms": 5, "bathrooms": 4.0,
     "sqft": 4500, "lot_size": "0.60 acres", "year_built": 2010,
     "description": "Stunning Rob Roy estate with a resort-style backyard and chef's kitchen.",
     "features": ["Resort Backyard", "Chef's Kitchen", "Library", "4-Car Garage", "Guest Casita"],
     "listing_status": "active", "lat": 30.2921, "lng": -97.8090},

    # --- More Suburban ---
    {"mls_id": "MLS-021", "address": "303 Wildflower Lane", "city": "Pflugerville", "state": "TX", "zip_code": "78660",
     "neighborhood": "Blackhawk", "property_type": "single_family", "price": 345000, "bedrooms": 4, "bathrooms": 2.5,
     "sqft": 2200, "lot_size": "0.18 acres", "year_built": 2017,
     "description": "Well-maintained family home with large game room and community amenities.",
     "features": ["Game Room", "Community Pool", "Walking Trails", "Good Schools"],
     "listing_status": "active", "lat": 30.4437, "lng": -97.6209},

    {"mls_id": "MLS-022", "address": "512 Limestone Way", "city": "Leander", "state": "TX", "zip_code": "78641",
     "neighborhood": "Crystal Falls", "property_type": "single_family", "price": 415000, "bedrooms": 4, "bathrooms": 3.0,
     "sqft": 2600, "lot_size": "0.22 acres", "year_built": 2021,
     "description": "New construction in Crystal Falls with open concept and large island kitchen.",
     "features": ["New Construction", "Island Kitchen", "Covered Patio", "Bonus Room"],
     "listing_status": "active", "lat": 30.5789, "lng": -97.8527},

    {"mls_id": "MLS-023", "address": "167 Magnolia Circle", "city": "Kyle", "state": "TX", "zip_code": "78640",
     "neighborhood": "Plum Creek", "property_type": "single_family", "price": 295000, "bedrooms": 3, "bathrooms": 2.0,
     "sqft": 1500, "lot_size": "0.12 acres", "year_built": 2019,
     "description": "Affordable starter home in growing community with golf course access.",
     "features": ["Golf Course Community", "Walking Trails", "Community Pool", "New Appliances"],
     "listing_status": "active", "lat": 29.9888, "lng": -97.8697},

    {"mls_id": "MLS-024", "address": "789 Brushy Creek Trail", "city": "Round Rock", "state": "TX", "zip_code": "78681",
     "neighborhood": "Walsh Ranch", "property_type": "single_family", "price": 525000, "bedrooms": 4, "bathrooms": 3.5,
     "sqft": 3000, "lot_size": "0.25 acres", "year_built": 2015,
     "description": "Premium Walsh Ranch home with three-car garage and outdoor living area.",
     "features": ["3-Car Garage", "Outdoor Kitchen", "Study", "Media Room", "Sprinkler System"],
     "listing_status": "active", "lat": 30.5543, "lng": -97.7291},

    {"mls_id": "MLS-025", "address": "234 Liberty Hill Road", "city": "Liberty Hill", "state": "TX", "zip_code": "78642",
     "neighborhood": "Santa Rita Ranch", "property_type": "single_family", "price": 390000, "bedrooms": 4, "bathrooms": 2.5,
     "sqft": 2350, "lot_size": "0.30 acres", "year_built": 2022,
     "description": "Master-planned community home with resort amenities and large lot.",
     "features": ["Resort Amenities", "Large Lot", "Open Floor Plan", "Energy Star Certified"],
     "listing_status": "active", "lat": 30.6654, "lng": -97.9213},

    # --- Different property types ---
    {"mls_id": "MLS-026", "address": "1200 East 6th Street", "city": "Austin", "state": "TX", "zip_code": "78702",
     "neighborhood": "East 6th", "property_type": "multi_family", "price": 750000, "bedrooms": 4, "bathrooms": 4.0,
     "sqft": 2800, "year_built": 2008,
     "description": "Duplex investment property with both units currently leased generating strong rental income.",
     "features": ["Duplex", "Investment Property", "Both Units Leased", "Separate Meters"],
     "listing_status": "active", "lat": 30.2634, "lng": -97.7205},

    {"mls_id": "MLS-027", "address": "45 Ranch Gate Road", "city": "Wimberley", "state": "TX", "zip_code": "78676",
     "neighborhood": "Wimberley Valley", "property_type": "land", "price": 985000, "bedrooms": 3, "bathrooms": 2.0,
     "sqft": 2200, "lot_size": "10 acres", "year_built": 1995,
     "description": "Ten-acre ranch property with creek frontage and Hill Country views. Perfect gentleman's ranch.",
     "features": ["10 Acres", "Creek Frontage", "Horse Ready", "Barn", "Hill Country Views"],
     "listing_status": "active", "lat": 29.9970, "lng": -98.0990},

    {"mls_id": "MLS-028", "address": "800 West Avenue Unit PH1", "city": "Austin", "state": "TX", "zip_code": "78701",
     "neighborhood": "Downtown", "property_type": "condo", "price": 1100000, "bedrooms": 3, "bathrooms": 3.0,
     "sqft": 2200, "year_built": 2022,
     "description": "Penthouse unit with wraparound terrace and unobstructed downtown skyline views.",
     "features": ["Penthouse", "Wraparound Terrace", "Skyline Views", "Private Elevator", "Wine Fridge"],
     "listing_status": "active", "lat": 30.2710, "lng": -97.7510},

    {"mls_id": "MLS-029", "address": "1600 Toro Grande Drive", "city": "Cedar Park", "state": "TX", "zip_code": "78613",
     "neighborhood": "Buttercup Creek", "property_type": "single_family", "price": 460000, "bedrooms": 4, "bathrooms": 3.0,
     "sqft": 2450, "lot_size": "0.19 acres", "year_built": 2013,
     "description": "Corner lot home with updated bathrooms and large backyard perfect for families.",
     "features": ["Corner Lot", "Updated Bathrooms", "Large Backyard", "Playroom", "Near Schools"],
     "listing_status": "active", "lat": 30.5119, "lng": -97.8140},

    {"mls_id": "MLS-030", "address": "333 Lamar Boulevard Unit 15", "city": "Austin", "state": "TX", "zip_code": "78705",
     "neighborhood": "North Campus", "property_type": "condo", "price": 275000, "bedrooms": 1, "bathrooms": 1.0,
     "sqft": 650, "year_built": 2012,
     "description": "Great investment condo near UT campus, currently rented with strong returns.",
     "features": ["Near UT Campus", "Investment Property", "Currently Leased", "Gated Community"],
     "listing_status": "active", "lat": 30.2909, "lng": -97.7460},

    # --- Additional variety ---
    {"mls_id": "MLS-031", "address": "2100 South First Street", "city": "Austin", "state": "TX", "zip_code": "78704",
     "neighborhood": "Bouldin Creek", "property_type": "single_family", "price": 725000, "bedrooms": 3, "bathrooms": 2.5,
     "sqft": 2000, "lot_size": "0.16 acres", "year_built": 2023,
     "description": "Brand new Bouldin Creek craftsman with designer finishes and separate studio.",
     "features": ["New Construction", "Designer Finishes", "Studio/ADU", "Custom Cabinets"],
     "listing_status": "active", "lat": 30.2430, "lng": -97.7606},

    {"mls_id": "MLS-032", "address": "750 Spicewood Springs Road", "city": "Austin", "state": "TX", "zip_code": "78759",
     "neighborhood": "Spicewood", "property_type": "single_family", "price": 545000, "bedrooms": 3, "bathrooms": 2.0,
     "sqft": 1950, "lot_size": "0.20 acres", "year_built": 2001,
     "description": "Well-kept Northwest Austin home near tech corridor with mature landscaping.",
     "features": ["Near Tech Corridor", "Mature Trees", "Updated HVAC", "Covered Deck"],
     "listing_status": "active", "lat": 30.4102, "lng": -97.7969},

    {"mls_id": "MLS-033", "address": "420 Anderson Mill Road", "city": "Austin", "state": "TX", "zip_code": "78729",
     "neighborhood": "Anderson Mill", "property_type": "single_family", "price": 380000, "bedrooms": 3, "bathrooms": 2.0,
     "sqft": 1600, "lot_size": "0.15 acres", "year_built": 2008,
     "description": "Move-in ready home with granite counters and stainless appliances in quiet neighborhood.",
     "features": ["Move-in Ready", "Granite Counters", "Stainless Appliances", "Cul-de-sac"],
     "listing_status": "active", "lat": 30.4508, "lng": -97.8015},

    {"mls_id": "MLS-034", "address": "160 West Parmer Lane", "city": "Austin", "state": "TX", "zip_code": "78717",
     "neighborhood": "Avery Ranch", "property_type": "single_family", "price": 495000, "bedrooms": 4, "bathrooms": 3.0,
     "sqft": 2700, "lot_size": "0.21 acres", "year_built": 2016,
     "description": "Avery Ranch gem with gorgeous pool and outdoor living near golf and trails.",
     "features": ["Pool", "Golf Community", "Hike/Bike Trails", "Open Floor Plan", "Study"],
     "listing_status": "active", "lat": 30.4934, "lng": -97.7886},

    {"mls_id": "MLS-035", "address": "88 Waller Street", "city": "Austin", "state": "TX", "zip_code": "78702",
     "neighborhood": "Holly", "property_type": "single_family", "price": 650000, "bedrooms": 3, "bathrooms": 2.0,
     "sqft": 1400, "lot_size": "0.10 acres", "year_built": 1960,
     "description": "Iconic Holly neighborhood cottage with massive development potential on a prime lot.",
     "features": ["Development Potential", "Prime Location", "Original Character", "Large Lot for Area"],
     "listing_status": "active", "lat": 30.2568, "lng": -97.7260},

    {"mls_id": "MLS-036", "address": "2500 Lake Austin Blvd", "city": "Austin", "state": "TX", "zip_code": "78703",
     "neighborhood": "Tarrytown", "property_type": "single_family", "price": 1450000, "bedrooms": 4, "bathrooms": 3.5,
     "sqft": 3400, "lot_size": "0.40 acres", "year_built": 2019,
     "description": "Stunning Tarrytown contemporary with lake access and designer kitchen.",
     "features": ["Lake Access", "Designer Kitchen", "Floor-to-Ceiling Windows", "Home Theater", "Saltwater Pool"],
     "listing_status": "active", "lat": 30.2912, "lng": -97.7800},

    {"mls_id": "MLS-037", "address": "1100 Exposition Boulevard", "city": "Austin", "state": "TX", "zip_code": "78703",
     "neighborhood": "Bryker Woods", "property_type": "single_family", "price": 975000, "bedrooms": 4, "bathrooms": 3.0,
     "sqft": 2800, "lot_size": "0.25 acres", "year_built": 2014,
     "description": "Charming Bryker Woods home with modern updates while maintaining neighborhood character.",
     "features": ["Modern Updates", "Neighborhood Character", "Walking Distance to Shops", "Screened Porch"],
     "listing_status": "active", "lat": 30.3015, "lng": -97.7560},

    {"mls_id": "MLS-038", "address": "600 Manor Road", "city": "Austin", "state": "TX", "zip_code": "78702",
     "neighborhood": "Cherrywood", "property_type": "townhouse", "price": 425000, "bedrooms": 2, "bathrooms": 2.5,
     "sqft": 1400, "year_built": 2021,
     "description": "Trendy Cherrywood townhome with rooftop deck and walkable lifestyle.",
     "features": ["Rooftop Deck", "Walkable", "Near UT", "Modern Finishes", "Private Garage"],
     "listing_status": "active", "lat": 30.2789, "lng": -97.7189},

    {"mls_id": "MLS-039", "address": "450 Bee Caves Road Unit 210", "city": "Austin", "state": "TX", "zip_code": "78746",
     "neighborhood": "Westlake", "property_type": "condo", "price": 495000, "bedrooms": 2, "bathrooms": 2.0,
     "sqft": 1300, "year_built": 2017,
     "description": "Westlake condo with hill country views, top-rated school district, and resort amenities.",
     "features": ["Hill Country Views", "Top Schools", "Resort Pool", "Fitness Center", "Gated"],
     "listing_status": "active", "lat": 30.2916, "lng": -97.7988},

    {"mls_id": "MLS-040", "address": "1800 East Oltorf Street", "city": "Austin", "state": "TX", "zip_code": "78741",
     "neighborhood": "Parker Lane", "property_type": "single_family", "price": 310000, "bedrooms": 2, "bathrooms": 1.0,
     "sqft": 950, "lot_size": "0.08 acres", "year_built": 1970,
     "description": "Affordable Austin starter home with great bones and value-add potential.",
     "features": ["Value-Add Potential", "Near Bus Lines", "Fenced Yard", "Covered Carport"],
     "listing_status": "active", "lat": 30.2336, "lng": -97.7261},

    # --- Pending/Sold for dashboard variety ---
    {"mls_id": "MLS-041", "address": "900 West Mary Street", "city": "Austin", "state": "TX", "zip_code": "78704",
     "neighborhood": "Travis Heights", "property_type": "single_family", "price": 780000, "bedrooms": 3, "bathrooms": 2.5,
     "sqft": 2100, "lot_size": "0.18 acres", "year_built": 2018,
     "description": "Beautiful Travis Heights home that went under contract quickly.",
     "features": ["Travis Heights", "Updated Kitchen", "Pool", "Near SoCo"],
     "listing_status": "pending", "lat": 30.2455, "lng": -97.7530},

    {"mls_id": "MLS-042", "address": "200 Barton Skyway", "city": "Austin", "state": "TX", "zip_code": "78704",
     "neighborhood": "Zilker", "property_type": "single_family", "price": 695000, "bedrooms": 3, "bathrooms": 2.0,
     "sqft": 1800, "lot_size": "0.14 acres", "year_built": 2010,
     "description": "Recently sold Zilker charmer near the park.",
     "features": ["Near Zilker Park", "Updated", "Hardwood Floors"],
     "listing_status": "sold", "lat": 30.2597, "lng": -97.7713},

    {"mls_id": "MLS-043", "address": "567 Shoal Creek Blvd", "city": "Austin", "state": "TX", "zip_code": "78757",
     "neighborhood": "Crestview", "property_type": "single_family", "price": 510000, "bedrooms": 3, "bathrooms": 2.0,
     "sqft": 1700, "lot_size": "0.15 acres", "year_built": 2006,
     "description": "Charming Crestview bungalow, recently sold above asking.",
     "features": ["Crestview", "Near MetroRail", "Bungalow Style"],
     "listing_status": "sold", "lat": 30.3395, "lng": -97.7378},
]
