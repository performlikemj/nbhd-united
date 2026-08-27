"""PII redaction policies and neural-engine label mappings.

The token-classification model (``lakshyakh93/deberta_finetuned_pii``)
emits ~60 labels covering names, addresses, phones, financial PII, IDs,
and more. We collapse them into a smaller set of canonical entity types
used by the placeholder system (PERSON, LOCATION, EMAIL_ADDRESS, etc.).

Presidio's pattern recognizers (credit card Luhn, IBAN checksum, email
regex fallback) live in ``apps/pii/engine.py:get_pattern_recognizers``.
"""

# Detector rollout contract. DeBERTa remains the safe production default;
# Liquid is retained only as an explicit, flag-gated option.
DEFAULT_DETECTOR_ENGINE = "deberta"
SUPPORTED_DETECTOR_ENGINES = frozenset({DEFAULT_DETECTOR_ENGINE, "liquid"})
DEFAULT_DETECTOR_TRANSPORT = "local"
SUPPORTED_DETECTOR_TRANSPORTS = frozenset({DEFAULT_DETECTOR_TRANSPORT, "shared"})


def resolve_positive_int(value, *, name: str) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if resolved <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return resolved


def resolve_detector_engine(requested: str | None) -> str:
    """Normalize a detector selection, falling back safely to DeBERTa."""
    normalized = (requested or "").strip().lower()
    if normalized in SUPPORTED_DETECTOR_ENGINES:
        return normalized
    return DEFAULT_DETECTOR_ENGINE


def resolve_detector_transport(requested: str | None) -> str:
    """Normalize a detector transport, falling back safely to local."""
    normalized = (requested or "").strip().lower()
    if normalized in SUPPORTED_DETECTOR_TRANSPORTS:
        return normalized
    return DEFAULT_DETECTOR_TRANSPORT


# Map the underlying model's token-classification labels to our internal
# entity types. The key name stays ``DEBERTA_LABEL_MAP`` for backwards
# compatibility with imports across the codebase; the underlying model
# has changed from ``onbekend/nbhd-pii-model`` to ``lakshyakh93/deberta_finetuned_pii``.
DEBERTA_LABEL_MAP = {
    # Personal names — collapsed to a single PERSON type so we can run
    # `_merge_adjacent_spans` to join "Sarah" (FIRSTNAME) + "Chen" (LASTNAME).
    "FIRSTNAME": "PERSON",
    "MIDDLENAME": "PERSON",
    "LASTNAME": "PERSON",
    "FULLNAME": "PERSON",
    "NAME": "PERSON",
    "PREFIX": "PERSON",
    "SUFFIX": "PERSON",
    "DISPLAYNAME": "PERSON",
    "ACCOUNTNAME": "PERSON",
    # USERNAME omitted on purpose: the model fires it on tokens like
    # "hunter" inside ``password is hunter2`` (probably training-data
    # bias). Keeping it would mint a [PERSON_N] for every such span.
    # Contact info
    "EMAIL": "EMAIL_ADDRESS",
    "PHONE_NUMBER": "PHONE_NUMBER",
    "PHONEIMEI": "PHONE_NUMBER",
    # Location — addresses + geo all collapsed
    "STREET": "LOCATION",
    "STREETADDRESS": "LOCATION",
    "SECONDARYADDRESS": "LOCATION",
    # BUILDINGNUMBER re-added WITH a raw-label guard in redactor._detect_pii:
    # a bare numeric span ("82", "82.5", "180 lbs") with no adjacent
    # street/address raw span is a measurement (body weight, lift number),
    # not an address — the guard skips it before this LOCATION collapse.
    # A number sitting next to a STREET/CITY/... span still redacts as part
    # of the merged address.
    "BUILDINGNUMBER": "LOCATION",
    "CITY": "LOCATION",
    "STATE": "LOCATION",
    "COUNTY": "LOCATION",
    "ZIPCODE": "LOCATION",
    "NEARBYGPSCOORDINATE": "LOCATION",
    "ORDINALDIRECTION": "LOCATION",
    # Financial
    "CREDITCARDNUMBER": "CREDIT_CARD",
    "CREDITCARDCVV": "CREDIT_CARD",
    "CREDITCARDISSUER": "CREDIT_CARD",
    "ACCOUNTNUMBER": "ACCOUNT",
    "BIC": "ACCOUNT",
    "IBAN": "IBAN_CODE",
    "BITCOINADDRESS": "CRYPTO_ADDRESS",
    "ETHEREUMADDRESS": "CRYPTO_ADDRESS",
    "LITECOINADDRESS": "CRYPTO_ADDRESS",
    # IDs and identifiers
    "PASSWORD": "PASSWORD",
    "PIN": "PASSWORD",
    # SSN omitted on purpose: the model fires it on ISO date headings
    # ("26-03-26" → SSN 0.87) and most of our tenants are international
    # — the false-positive rate dwarfs the true-positive value. If a US
    # tenant pastes a real SSN, the FIRSTNAME/LASTNAME context usually
    # gets the rest redacted anyway.
    "MASKEDNUMBER": "ACCOUNT",
    "VEHICLEVIN": "ID_DOCUMENT",
    "VEHICLEVRM": "ID_DOCUMENT",
    # Network identifiers
    "IP": "IP_ADDRESS",
    "IPV4": "IP_ADDRESS",
    "IPV6": "IP_ADDRESS",
    "MAC": "IP_ADDRESS",
    "USERAGENT": "IP_ADDRESS",
    # DATE stays OUT of the static map on purpose: the model fires it on every
    # yyyy-mm-dd journal-entry heading, so a blanket DATE->something mapping would
    # redact every date. Birth dates ARE identifying PII, though, so
    # redactor._detect_pii promotes a raw DATE span to DATE_OF_BIRTH only when a
    # birth-context cue ("date of birth", "born", "生年月日", "誕生日") sits beside
    # it — an ordinary calendar date still falls through here untouched.
    # Note: model also emits AMOUNT, CURRENCY, DATE, TIME, JOBAREA,
    # JOBDESCRIPTOR, JOBTITLE, JOBTYPE, COMPANY_NAME, NUMBER, URL, GENDER,
    # SEX, SEXTYPE. We intentionally drop those — they're context, not
    # identifying PII. (NUMBER in particular fires on credit-card digits
    # we already catch via Presidio's Luhn-validated CreditCardRecognizer.)
}

# LiquidAI's pinned detector emits these 40 domain-qualified types after its
# shipped hybrid decoder. ``None`` is deliberate: those values are sensitive
# context or broad business metadata rather than identifying PII, and the
# existing placeholder taxonomy has no honest destination for them. Identifier,
# name-like, and location-like types fail closed into the closest existing type.
LIQUID_LABEL_MAP = {
    # Identity
    "identity.person_name": "PERSON",
    "identity.ssn": "ID_DOCUMENT",
    "identity.national_id": "ID_DOCUMENT",
    "identity.passport": "ID_DOCUMENT",
    "identity.drivers_license": "ID_DOCUMENT",
    "identity.date_of_birth": "DATE_OF_BIRTH",
    "identity.tax_id": "ID_DOCUMENT",
    # Contact and location
    "contact.email": "EMAIL_ADDRESS",
    "contact.phone": "PHONE_NUMBER",
    "contact.address": "LOCATION",
    "contact.postal_code": "LOCATION",
    "contact.ip_address": "IP_ADDRESS",
    "location.gps_coordinates": "LOCATION",
    # Financial
    "financial.credit_card": "CREDIT_CARD",
    "financial.iban": "IBAN_CODE",
    "financial.bank_account": "ACCOUNT",
    "financial.swift_bic": "ACCOUNT",
    "financial.crypto_wallet": "CRYPTO_ADDRESS",
    "financial.amount": None,
    # Credentials
    "credential.api_key": "PASSWORD",
    "credential.password": "PASSWORD",
    "credential.private_key": "PASSWORD",
    "credential.jwt": "PASSWORD",
    "credential.connection_string": "PASSWORD",
    "developer.login_credentials": "PASSWORD",
    # Online and device identifiers
    "online.username": "PERSON",
    "online.url": None,
    "device.mac_address": "IP_ADDRESS",
    "device.imei": "PHONE_NUMBER",
    "developer.device_id": "ID_DOCUMENT",
    # Healthcare identifiers; conditions and treatments remain contextual.
    "healthcare.medical_record": "ID_DOCUMENT",
    "healthcare.health_plan_id": "ID_DOCUMENT",
    "healthcare.condition": None,
    "healthcare.medication": None,
    # Organization, special-category context, and legal identifiers.
    "org.company_name": None,
    "special.religion": None,
    "special.political": None,
    "special.orientation": None,
    "special.health_status": None,
    "legal.case_number": "ID_DOCUMENT",
}

# ``redactor.py`` intentionally remains unchanged and imports the historical
# DEBERTA_LABEL_MAP name. Extend that lookup with mapped Liquid labels so either
# engine produces the same canonical entity types at the existing seam.
DEBERTA_LABEL_MAP.update(
    {raw_label: entity_type for raw_label, entity_type in LIQUID_LABEL_MAP.items() if entity_type is not None}
)

# Raw labels that signal real street/address context around a BUILDINGNUMBER
# hit (see redactor._detect_pii). Derived from the map so it can never drift:
# every raw label that collapses to LOCATION except BUILDINGNUMBER itself —
# a bare number must not vouch for another bare number.
ADDRESS_CONTEXT_LABELS = frozenset(
    raw for raw, mapped in DEBERTA_LABEL_MAP.items() if mapped == "LOCATION" and raw != "BUILDINGNUMBER"
)

# Per-label minimum-score overrides, keyed by the model's RAW label (checked
# before DEBERTA_LABEL_MAP collapses it). A detection must clear both the
# tier's ``score_threshold`` AND any override here. Used to keep a label whose
# true positives are high-confidence but whose false positives cluster just
# above the global threshold — e.g. PIN fires near 0.5 on bare lift numbers,
# but a real "my PIN is 4821" lands well above 0.7, so the override keeps
# genuine PINs redacting while dropping the marginal fitness-number hits.
LABEL_SCORE_OVERRIDES = {
    "PIN": 0.7,
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
            # DATE_OF_BIRTH is context-gated, not statically mapped: the model's
            # raw DATE label stays dropped in DEBERTA_LABEL_MAP (it fires on every
            # journal date heading), and redactor._detect_pii promotes a DATE span
            # to DATE_OF_BIRTH only under a birth-context cue. Listing it here is
            # what lets that promoted span clear the tier's entities gate.
            "DATE_OF_BIRTH",
            "PASSWORD",
            "IP_ADDRESS",
            "ID_DOCUMENT",
            "ACCOUNT",
            "CRYPTO_ADDRESS",
        ],
        # 0.5 calibrated for lakshyakh93/deberta_finetuned_pii: full names
        # near 0.99, single first/last names land in 0.5–0.7 depending on
        # context. The old model was calibrated for 0.7; the new model's
        # softmax distribution sits a bit lower across the board.
        "score_threshold": 0.5,
    },
}
