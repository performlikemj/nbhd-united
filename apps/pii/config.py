"""PII redaction policies and label mappings.

DeBERTa model handles contextual PII (names, addresses, dates, passwords).
Presidio pattern recognizers handle financial PII (credit cards, IBANs).
"""

# Map DeBERTa model output labels to internal entity types used in placeholders.
# Multiple model labels consolidate into one entity type (e.g., GIVENNAME + SURNAME → PERSON).
DEBERTA_LABEL_MAP = {
    "GIVENNAME": "PERSON",
    "SURNAME": "PERSON",
    "USERNAME": "PERSON",
    "EMAIL": "EMAIL_ADDRESS",
    "TELEPHONENUM": "PHONE_NUMBER",
    "CREDITCARDNUMBER": "CREDIT_CARD",
    "ACCOUNTNUM": "ACCOUNT",
    "STREET": "LOCATION",
    "CITY": "LOCATION",
    "ZIPCODE": "LOCATION",
    "BUILDINGNUM": "LOCATION",
    "DATEOFBIRTH": "DATE_OF_BIRTH",
    "PASSWORD": "PASSWORD",
    "TAXNUM": "TAX_NUMBER",
    "SOCIALNUM": "SOCIAL_NUMBER",
    "IPV4": "IP_ADDRESS",
    "IPV6": "IP_ADDRESS",
    "DRIVERLICENSENUM": "ID_DOCUMENT",
    "IDCARDNUM": "ID_DOCUMENT",
    "PASSPORT": "ID_DOCUMENT",
}

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
            "DATE_OF_BIRTH",
            "PASSWORD",
            "IP_ADDRESS",
            "ID_DOCUMENT",
            "ACCOUNT",
            "TAX_NUMBER",
            "SOCIAL_NUMBER",
        ],
        "score_threshold": 0.7,
    },
}
