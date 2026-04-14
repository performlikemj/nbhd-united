"""Country and timezone data for onboarding.

Structured as: country → timezone (single) or country → {zone_label: timezone} (multi).
Only countries with multiple timezones need drill-down.
"""

from __future__ import annotations

# Countries with a single timezone: country_name → IANA timezone
# Sorted roughly by population / likelihood of use
SINGLE_TZ_COUNTRIES: dict[str, str] = {
    "Japan": "Asia/Tokyo",
    "South Korea": "Asia/Seoul",
    "India": "Asia/Kolkata",
    "China": "Asia/Shanghai",
    "Thailand": "Asia/Bangkok",
    "Vietnam": "Asia/Ho_Chi_Minh",
    "Philippines": "Asia/Manila",
    "Singapore": "Asia/Singapore",
    "Malaysia": "Asia/Kuala_Lumpur",
    "Indonesia": "Asia/Jakarta",  # simplified — has multiple but Jakarta covers most
    "Taiwan": "Asia/Taipei",
    "Hong Kong": "Asia/Hong_Kong",
    "United Kingdom": "Europe/London",
    "France": "Europe/Paris",
    "Germany": "Europe/Berlin",
    "Italy": "Europe/Rome",
    "Spain": "Europe/Madrid",
    "Netherlands": "Europe/Amsterdam",
    "Belgium": "Europe/Brussels",
    "Switzerland": "Europe/Zurich",
    "Sweden": "Europe/Stockholm",
    "Norway": "Europe/Oslo",
    "Denmark": "Europe/Copenhagen",
    "Finland": "Europe/Helsinki",
    "Poland": "Europe/Warsaw",
    "Czech Republic": "Europe/Prague",
    "Austria": "Europe/Vienna",
    "Ireland": "Europe/Dublin",
    "Greece": "Europe/Athens",
    "Turkey": "Europe/Istanbul",
    "Israel": "Asia/Jerusalem",
    "UAE": "Asia/Dubai",
    "Saudi Arabia": "Asia/Riyadh",
    "Egypt": "Africa/Cairo",
    "South Africa": "Africa/Johannesburg",
    "Nigeria": "Africa/Lagos",
    "Kenya": "Africa/Nairobi",
    "Ghana": "Africa/Accra",
    "Ethiopia": "Africa/Addis_Ababa",
    "Morocco": "Africa/Casablanca",
    "Jamaica": "America/Jamaica",
    "Trinidad": "America/Port_of_Spain",
    "Colombia": "America/Bogota",
    "Peru": "America/Lima",
    "Chile": "America/Santiago",
    "Argentina": "America/Argentina/Buenos_Aires",
    "Venezuela": "America/Caracas",
    "Costa Rica": "America/Costa_Rica",
    "Panama": "America/Panama",
    "Cuba": "America/Havana",
    "Dominican Republic": "America/Santo_Domingo",
    "Puerto Rico": "America/Puerto_Rico",
    "New Zealand": "Pacific/Auckland",
    "Pakistan": "Asia/Karachi",
    "Bangladesh": "Asia/Dhaka",
    "Sri Lanka": "Asia/Colombo",
    "Nepal": "Asia/Kathmandu",
    "Myanmar": "Asia/Yangon",
    "Cambodia": "Asia/Phnom_Penh",
    "Laos": "Asia/Vientiane",
}

# Countries with multiple timezones: country → {label: timezone}
MULTI_TZ_COUNTRIES: dict[str, dict[str, str]] = {
    "United States": {
        "Eastern (NYC, Miami, Atlanta)": "America/New_York",
        "Central (Chicago, Dallas, Houston)": "America/Chicago",
        "Mountain (Denver, Phoenix)": "America/Denver",
        "Pacific (LA, Seattle, SF)": "America/Los_Angeles",
        "Alaska": "America/Anchorage",
        "Hawaii": "Pacific/Honolulu",
    },
    "Canada": {
        "Eastern (Toronto, Montreal)": "America/Toronto",
        "Central (Winnipeg)": "America/Winnipeg",
        "Mountain (Calgary, Edmonton)": "America/Edmonton",
        "Pacific (Vancouver)": "America/Vancouver",
        "Atlantic (Halifax)": "America/Halifax",
    },
    "Australia": {
        "Eastern (Sydney, Melbourne)": "Australia/Sydney",
        "Central (Adelaide)": "Australia/Adelaide",
        "Western (Perth)": "Australia/Perth",
        "Queensland (Brisbane)": "Australia/Brisbane",
    },
    "Brazil": {
        "Brasília (São Paulo, Rio)": "America/Sao_Paulo",
        "Amazon (Manaus)": "America/Manaus",
        "Northeast (Recife)": "America/Recife",
    },
    "Russia": {
        "Moscow": "Europe/Moscow",
        "Yekaterinburg": "Asia/Yekaterinburg",
        "Novosibirsk": "Asia/Novosibirsk",
        "Vladivostok": "Asia/Vladivostok",
    },
    "Mexico": {
        "Central (Mexico City)": "America/Mexico_City",
        "Pacific (Tijuana)": "America/Tijuana",
        "Mountain (Chihuahua)": "America/Chihuahua",
    },
}

# All countries combined for lookup
ALL_COUNTRIES: dict[str, str | dict[str, str]] = {
    **{k: v for k, v in SINGLE_TZ_COUNTRIES.items()},
    **{k: v for k, v in MULTI_TZ_COUNTRIES.items()},
}

# Localized country names for common languages
COUNTRY_NAMES_I18N: dict[str, dict[str, str]] = {
    "ja": {
        "Japan": "日本",
        "South Korea": "韓国",
        "China": "中国",
        "Taiwan": "台湾",
        "United States": "アメリカ",
        "United Kingdom": "イギリス",
        "Australia": "オーストラリア",
        "Canada": "カナダ",
        "France": "フランス",
        "Germany": "ドイツ",
        "India": "インド",
        "Thailand": "タイ",
        "Philippines": "フィリピン",
        "Singapore": "シンガポール",
        "Brazil": "ブラジル",
        "Russia": "ロシア",
        "Mexico": "メキシコ",
    },
    "es": {
        "United States": "Estados Unidos",
        "United Kingdom": "Reino Unido",
        "Germany": "Alemania",
        "France": "Francia",
        "Japan": "Japón",
        "South Korea": "Corea del Sur",
        "China": "China",
        "Brazil": "Brasil",
        "Mexico": "México",
        "Spain": "España",
        "Colombia": "Colombia",
        "Argentina": "Argentina",
    },
}


def get_country_display(country: str, lang: str = "en") -> str:
    """Get localized country name, with flag emoji."""
    i18n = COUNTRY_NAMES_I18N.get(lang, {})
    display = i18n.get(country, country)
    flag = COUNTRY_FLAGS.get(country, "")
    return f"{flag} {display}".strip()


# Country flag emojis
COUNTRY_FLAGS: dict[str, str] = {
    "Japan": "🇯🇵",
    "South Korea": "🇰🇷",
    "China": "🇨🇳",
    "Taiwan": "🇹🇼",
    "India": "🇮🇳",
    "Thailand": "🇹🇭",
    "Vietnam": "🇻🇳",
    "Philippines": "🇵🇭",
    "Singapore": "🇸🇬",
    "Malaysia": "🇲🇾",
    "Indonesia": "🇮🇩",
    "Hong Kong": "🇭🇰",
    "United States": "🇺🇸",
    "Canada": "🇨🇦",
    "Mexico": "🇲🇽",
    "United Kingdom": "🇬🇧",
    "France": "🇫🇷",
    "Germany": "🇩🇪",
    "Italy": "🇮🇹",
    "Spain": "🇪🇸",
    "Netherlands": "🇳🇱",
    "Belgium": "🇧🇪",
    "Switzerland": "🇨🇭",
    "Sweden": "🇸🇪",
    "Norway": "🇳🇴",
    "Denmark": "🇩🇰",
    "Finland": "🇫🇮",
    "Poland": "🇵🇱",
    "Ireland": "🇮🇪",
    "Greece": "🇬🇷",
    "Turkey": "🇹🇷",
    "Australia": "🇦🇺",
    "New Zealand": "🇳🇿",
    "Brazil": "🇧🇷",
    "Argentina": "🇦🇷",
    "Colombia": "🇨🇴",
    "Chile": "🇨🇱",
    "Jamaica": "🇯🇲",
    "Trinidad": "🇹🇹",
    "Nigeria": "🇳🇬",
    "Kenya": "🇰🇪",
    "South Africa": "🇿🇦",
    "Egypt": "🇪🇬",
    "UAE": "🇦🇪",
    "Saudi Arabia": "🇸🇦",
    "Israel": "🇮🇱",
    "Russia": "🇷🇺",
    "Pakistan": "🇵🇰",
    "Bangladesh": "🇧🇩",
}


def get_popular_countries(lang: str = "en") -> list[str]:
    """Return popular countries ordered by likely relevance for a language."""
    # Language-specific ordering
    if lang == "ja":
        return [
            "Japan",
            "United States",
            "South Korea",
            "China",
            "Taiwan",
            "Thailand",
            "Australia",
            "United Kingdom",
            "Canada",
            "Philippines",
        ]
    if lang == "es":
        return [
            "Mexico",
            "Spain",
            "Colombia",
            "Argentina",
            "United States",
            "Chile",
            "Peru",
            "Venezuela",
            "Cuba",
            "Dominican Republic",
        ]
    # Default (English / global)
    return [
        "United States",
        "United Kingdom",
        "Canada",
        "Australia",
        "India",
        "Japan",
        "Germany",
        "France",
        "Brazil",
        "Philippines",
    ]


def build_country_keyboard(lang: str = "en") -> list[list[dict[str, str]]]:
    """Build inline keyboard with popular countries + Other button."""
    popular = get_popular_countries(lang)
    rows: list[list[dict[str, str]]] = []

    # 2 buttons per row
    for i in range(0, len(popular), 2):
        row = []
        for country in popular[i : i + 2]:
            display = get_country_display(country, lang)
            row.append({"text": display, "callback_data": f"tz_country:{country}"})
        rows.append(row)

    # "Other" button
    other_text = "その他..." if lang == "ja" else "Otro..." if lang == "es" else "Other..."
    rows.append([{"text": other_text, "callback_data": "tz_country:OTHER"}])

    return rows


def build_zone_keyboard(country: str) -> list[list[dict[str, str]]]:
    """Build inline keyboard for multi-timezone country drill-down."""
    zones = MULTI_TZ_COUNTRIES.get(country, {})
    rows: list[list[dict[str, str]]] = []
    for label, tz in zones.items():
        rows.append([{"text": label, "callback_data": f"tz_zone:{tz}"}])
    return rows


def resolve_country_timezone(country: str) -> str | dict[str, str] | None:
    """Resolve a country to its timezone(s).

    Returns:
        str: Single timezone (done)
        dict: Multiple timezones (need drill-down)
        None: Country not found
    """
    # Exact match
    if country in ALL_COUNTRIES:
        return ALL_COUNTRIES[country]

    # Case-insensitive search
    country_lower = country.lower()
    for name, tz in ALL_COUNTRIES.items():
        if name.lower() == country_lower:
            return tz

    # Fuzzy / partial match
    for name, tz in ALL_COUNTRIES.items():
        if country_lower in name.lower() or name.lower() in country_lower:
            return tz

    return None
