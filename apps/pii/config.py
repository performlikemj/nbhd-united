"""Tier-based PII redaction policies.

Starter tier routes through OpenRouter (third-party aggregator) — highest risk.
Premium tier goes direct to Anthropic (DPA in place) — lower risk.
BYOK users bring their own keys — they accept the risk.
"""

TIER_POLICIES = {
    "starter": {
        "enabled": True,
        "entities": [
            "PERSON",
            "EMAIL_ADDRESS",
            "PHONE_NUMBER",
            "CREDIT_CARD",
            "IBAN_CODE",
            "LOCATION",
        ],
        "score_threshold": 0.7,
    },
    "premium": {
        "enabled": True,
        "entities": [
            "CREDIT_CARD",
            "IBAN_CODE",
            "PHONE_NUMBER",
        ],
        "score_threshold": 0.8,
    },
    "byok": {
        "enabled": False,
        "entities": [],
        "score_threshold": 0.8,
    },
}

# Country/region names that Presidio often misidentifies as PERSON
COUNTRY_DENYLIST = {
    "jordan", "georgia", "chad", "mali", "india", "china", "japan",
    "korea", "israel", "ireland", "turkey", "cuba", "niger", "cameron",
    "santiago", "victoria", "florence", "austin", "phoenix", "savannah",
    "virginia", "carolina", "dakota", "montana", "orlando", "columbus",
}
