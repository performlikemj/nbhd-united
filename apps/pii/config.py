"""PII redaction policies and label mappings.

The token-classification model (``lakshyakh93/deberta_finetuned_pii``)
emits ~60 labels covering names, addresses, phones, financial PII, IDs,
and more. We collapse them into a smaller set of canonical entity types
used by the placeholder system (PERSON, LOCATION, EMAIL_ADDRESS, etc.).

Presidio's pattern recognizers (credit card Luhn, IBAN checksum, email
regex fallback) live in ``apps/pii/engine.py:get_pattern_recognizers``.
"""

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
    # BUILDINGNUMBER omitted on purpose: a bare building number is
    # indistinguishable from a lift weight/rep count ("benched 225",
    # "squatted 140") and fired [LOCATION_N] over fitness logs fleet-wide.
    # Street and city names still redact via STREET/STREETADDRESS/CITY, which
    # carry the actual identifying token; the number alone is not load-bearing.
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
    # DATE omitted on purpose: the model fires on every yyyy-mm-dd
    # journal-entry heading. Birth-date redaction was nice-to-have, not
    # load-bearing — Presidio doesn't catch it either and starter-tier
    # tenants haven't been complaining.
    # Note: model also emits AMOUNT, CURRENCY, DATE, TIME, JOBAREA,
    # JOBDESCRIPTOR, JOBTITLE, JOBTYPE, COMPANY_NAME, NUMBER, URL, GENDER,
    # SEX, SEXTYPE. We intentionally drop those — they're context, not
    # identifying PII. (NUMBER in particular fires on credit-card digits
    # we already catch via Presidio's Luhn-validated CreditCardRecognizer.)
}

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
            # DATE_OF_BIRTH intentionally omitted: no detector can emit it (the
            # model's DATE label is deliberately dropped above to avoid firing on
            # every yyyy-mm-dd journal heading), so listing it only advertised
            # coverage that never happened. See DEBERTA_LABEL_MAP notes.
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
