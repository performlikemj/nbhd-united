"""Weather URL generation for tenant briefing prompts.

Maps IANA timezones to approximate coordinates for Open-Meteo API.
These are capital/major city coordinates — good enough for weather
since weather doesn't change dramatically within a timezone.
"""

from __future__ import annotations

from urllib.parse import urlencode

# Timezone → (latitude, longitude) mapping
# Uses capital/major city of each timezone region
TIMEZONE_COORDS: dict[str, tuple[float, float]] = {
    # Americas
    "America/New_York": (40.71, -74.01),
    "America/Chicago": (41.88, -87.63),
    "America/Denver": (39.74, -104.99),
    "America/Los_Angeles": (34.05, -118.24),
    "America/Anchorage": (61.22, -149.90),
    "Pacific/Honolulu": (21.31, -157.86),
    "America/Toronto": (43.65, -79.38),
    "America/Vancouver": (49.28, -123.12),
    "America/Mexico_City": (19.43, -99.13),
    "America/Sao_Paulo": (-23.55, -46.63),
    "America/Argentina/Buenos_Aires": (-34.60, -58.38),
    "America/Bogota": (4.71, -74.07),
    "America/Lima": (-12.05, -77.04),
    "America/Santiago": (-33.45, -70.67),
    "America/Jamaica": (18.11, -76.79),
    "America/Puerto_Rico": (18.47, -66.11),
    # Europe
    "Europe/London": (51.51, -0.13),
    "Europe/Paris": (48.86, 2.35),
    "Europe/Berlin": (52.52, 13.41),
    "Europe/Madrid": (40.42, -3.70),
    "Europe/Rome": (41.90, 12.50),
    "Europe/Amsterdam": (52.37, 4.90),
    "Europe/Moscow": (55.76, 37.62),
    "Europe/Istanbul": (41.01, 28.98),
    "Europe/Warsaw": (52.23, 21.01),
    # Asia
    "Asia/Tokyo": (34.69, 135.50),  # Osaka (common for Japan users)
    "Asia/Seoul": (37.57, 126.98),
    "Asia/Shanghai": (31.23, 121.47),
    "Asia/Hong_Kong": (22.32, 114.17),
    "Asia/Taipei": (25.03, 121.57),
    "Asia/Singapore": (1.35, 103.82),
    "Asia/Bangkok": (13.76, 100.50),
    "Asia/Jakarta": (-6.21, 106.85),
    "Asia/Manila": (14.60, 120.98),
    "Asia/Kuala_Lumpur": (3.14, 101.69),
    "Asia/Kolkata": (28.61, 77.21),  # Delhi
    "Asia/Dubai": (25.20, 55.27),
    # Oceania
    "Australia/Sydney": (-33.87, 151.21),
    "Australia/Melbourne": (-37.81, 144.96),
    "Pacific/Auckland": (-36.85, 174.76),
    # Africa
    "Africa/Cairo": (30.04, 31.24),
    "Africa/Lagos": (6.52, 3.38),
    "Africa/Nairobi": (-1.29, 36.82),
    "Africa/Johannesburg": (-26.20, 28.04),
    # UTC offset fallbacks (Etc/GMT±N)
    "UTC": (51.51, -0.13),  # London as default
}


def get_coords_for_timezone(tz: str) -> tuple[float, float]:
    """Get approximate coordinates for an IANA timezone.

    Falls back to London (UTC) if timezone is unknown.
    """
    if tz in TIMEZONE_COORDS:
        return TIMEZONE_COORDS[tz]

    # Try Etc/GMT offsets → map to a reasonable city
    if tz.startswith("Etc/GMT"):
        return TIMEZONE_COORDS.get("UTC", (51.51, -0.13))

    # Fuzzy match: try the timezone's region prefix
    region = tz.split("/")[0] if "/" in tz else ""
    region_defaults = {
        "America": (40.71, -74.01),  # NYC
        "Europe": (51.51, -0.13),  # London
        "Asia": (34.69, 135.50),  # Osaka
        "Africa": (6.52, 3.38),  # Lagos
        "Australia": (-33.87, 151.21),  # Sydney
        "Pacific": (-36.85, 174.76),  # Auckland
    }
    return region_defaults.get(region, (51.51, -0.13))


def build_weather_url_from_coords(lat: float, lon: float, tz: str) -> str:
    """Build an Open-Meteo forecast URL from explicit coordinates."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,weather_code,precipitation_probability,precipitation,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": tz,
        "forecast_days": 3,
    }
    return f"https://api.open-meteo.com/v1/forecast?{urlencode(params)}"


def build_weather_url(tz: str) -> str:
    """Build an Open-Meteo forecast URL for the given timezone.

    Uses approximate coordinates from timezone → city mapping.
    """
    lat, lon = get_coords_for_timezone(tz)
    return build_weather_url_from_coords(lat, lon, tz)
