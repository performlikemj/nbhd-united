"""PII redaction policies.

All traffic routes through OpenRouter (third-party aggregator) — redact PII.
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
}

# Country/region names that Presidio often misidentifies as PERSON
COUNTRY_DENYLIST = {
    "jordan", "georgia", "chad", "mali", "india", "china", "japan",
    "korea", "israel", "ireland", "turkey", "cuba", "niger", "cameron",
    "santiago", "victoria", "florence", "austin", "phoenix", "savannah",
    "virginia", "carolina", "dakota", "montana", "orlando", "columbus",
}
