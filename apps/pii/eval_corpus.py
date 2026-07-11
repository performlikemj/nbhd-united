"""Synthetic PII-redaction evaluation corpus (Eval Suite 3).

Every case here is 100% invented — names, addresses, phone numbers, and
emails are synthetic (test-vector numbers or `example.com`/`.org`/`.io`
domains). None reference a real person.

Canonical entry point under test
---------------------------------
``apps.pii.redactor.redact_user_message`` is the single seam every inbound
channel calls BEFORE a user's text reaches OpenClaw / the LLM provider —
this repo's actual egress-to-LLM boundary for user-authored text:

- ``apps/router/poller.py:1432`` (Telegram long-poll)
- ``apps/router/line_webhook.py:1317-1318`` (LINE webhook)
- ``apps/router/chat_views.py:480`` (iOS chat)

``apps/pii/test_eval_corpus.py`` drives every case in this module through
that exact function via ``CASES`` and ``SEQUENCE_CASES``.

A supplementary ``KNOWN_ENTITY_CASES`` table also covers the REUSE-ONLY entry
points — ``redact_known_entities`` and ``RedactionSession`` — used by
agent-authored/tool-response paths (workspace memory sync, friends scrub,
copilot) that never reach ``redact_user_message``. Folded in from a sibling
corpus attempt (PR #1144) that landed on this same branch in parallel; kept
because it is real, working coverage of call paths this module's main tables
do not exercise.

Detection-mocking convention (mirrors apps/pii/tests.py)
---------------------------------------------------------
``apps/pii/tests.py`` already establishes the pattern this corpus follows
for anything that depends on the neural DeBERTa model: mock
``apps.pii.engine.get_pii_pipeline`` to return a stand-in that emits
caller-supplied ``(raw_label, word, score)`` hits (see ``_fake_pipeline`` in
that file, used by e.g. ``FitnessFalsePositiveTest``,
``BuildingNumberMeasurementGuardTest``, ``PhoneRecognizerTest``,
``PiiReuseTelemetryTest``). Only ONE class in that file
(``RedactTextIntegrationTest``) exercises the real 554MB model, and it
self-skips when the model isn't present — CI's ``backend-test`` job never
installs the model weights, so that class does not run in CI today. This
corpus follows the DOMINANT, CI-live convention: cases that depend on the
neural model's raw per-span label carry a ``raw_hits`` tuple that is fed to
a fake pipeline (see ``test_eval_corpus.py``); real, unmocked Presidio
pattern recognizers (email/phone/credit-card/IBAN — pure regex + checksum,
no model weights needed) still run for real on every case, so anything
relying on them is exercising actual production code, not a stub.

Caveat this implies for the ``jp`` cases: they prove the redaction/guard/
placeholder MACHINERY handles multi-byte Japanese text correctly (offsets,
casefold, merge-adjacent-spans, placeholder assembly) given a raw model
label — they do NOT validate whether the real DeBERTa model (fine-tuned on
the English-centric ai4privacy dataset) actually detects Japanese names/
addresses at that confidence in production. That question needs the real
model (see ``RedactTextIntegrationTest`` pattern) and is out of scope here;
flagged in the PR body as a follow-up, not silently assumed.

Known-gap cases
----------------
This repo's test runner is Django's ``manage.py test`` (plain ``unittest``),
not pytest — there is no ``pytest.mark.xfail`` available. A case with
``known_gap`` set encodes "this currently FAILS, and that is a real,
already-documented production limitation" by asserting the assertion itself
raises ``AssertionError`` (see ``test_eval_corpus.py``). If the underlying
code is later fixed, that wrapped ``assertRaises`` itself fails loudly —
forcing the corpus to be updated rather than silently going stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RawHit:
    """One simulated raw model span, in the shape ``_fake_pipeline`` (see
    ``apps/pii/tests.py``) consumes: the model's RAW label (pre
    ``DEBERTA_LABEL_MAP`` collapse), the exact substring to locate via
    ``text.find``, and a confidence score."""

    raw_label: str
    word: str
    score: float


@dataclass(frozen=True)
class EvalCase:
    id: str
    text: str
    tags: tuple[str, ...]
    expected_redacted: tuple[str, ...] = ()
    expected_preserved: tuple[str, ...] = ()
    raw_hits: tuple[RawHit, ...] = ()
    expected_exact: str | None = None
    tenant_display_name: str | None = None
    seed_entity_map: dict[str, Any] | None = None
    expect_clean_placeholders: bool = False
    known_gap: str | None = None
    notes: str = ""
    # Every case in this corpus is invented (see module docstring) — always
    # True today. Kept as an explicit field, not a module-level assumption,
    # so Wave A's PR-A2 exclusion mechanism has a stable `bool` attribute to
    # filter on across corpora — field name/type agreed with the
    # upload-security corpus (Suite 2) for Wave E aggregation.
    is_synthetic: bool = True


def _c(
    id: str,
    text: str,
    *,
    tags: tuple[str, ...],
    redacted: tuple[str, ...] = (),
    preserved: tuple[str, ...] = (),
    hits: tuple[tuple[str, str, float], ...] = (),
    exact: str | None = None,
    display_name: str | None = None,
    seed_map: dict[str, Any] | None = None,
    clean_placeholders: bool = False,
    known_gap: str | None = None,
    notes: str = "",
) -> EvalCase:
    return EvalCase(
        id=id,
        text=text,
        tags=tags,
        expected_redacted=redacted,
        expected_preserved=preserved,
        raw_hits=tuple(RawHit(*h) for h in hits),
        expected_exact=exact,
        tenant_display_name=display_name,
        seed_entity_map=seed_map,
        expect_clean_placeholders=clean_placeholders,
        known_gap=known_gap,
        notes=notes,
    )


CASES: list[EvalCase] = [
    # -------------------------------------------------------------------
    # INCIDENT: weight/measurement false positives
    # (redactor.py _BARE_MEASUREMENT_RE / BUILDINGNUMBER adjacency guard)
    # -------------------------------------------------------------------
    _c(
        "weight_kg_bare_not_redacted",
        "my weight is 82kg",
        tags=("incident", "weight_measurement"),
        preserved=("my weight is 82kg",),
        hits=(("BUILDINGNUMBER", "82kg", 0.8),),
        exact="my weight is 82kg",
    ),
    _c(
        "weight_lbs_space_not_redacted",
        "weighed 180 lbs this morning",
        tags=("incident", "weight_measurement"),
        hits=(("BUILDINGNUMBER", "180 lbs", 0.75),),
        exact="weighed 180 lbs this morning",
    ),
    _c(
        "weight_bare_integer_not_redacted",
        "my weight is 82",
        tags=("incident", "weight_measurement"),
        hits=(("BUILDINGNUMBER", "82", 0.8),),
        exact="my weight is 82",
    ),
    _c(
        "weight_decimal_not_redacted",
        "today's reading: 82.5",
        tags=("incident", "weight_measurement"),
        hits=(("BUILDINGNUMBER", "82.5", 0.9),),
        exact="today's reading: 82.5",
    ),
    _c(
        "weight_intl_comma_decimal_not_redacted",
        "weight 82,5 kg",
        tags=("incident", "weight_measurement"),
        notes="German-style decimal weight; intl tenant base regression guard.",
        hits=(("BUILDINGNUMBER", "82,5", 0.8),),
        exact="weight 82,5 kg",
    ),
    _c(
        "lift_number_not_redacted",
        "I benched 225",
        tags=("incident", "weight_measurement", "fitness_garbling"),
        hits=(("BUILDINGNUMBER", "225", 0.707),),
        exact="I benched 225",
    ),
    _c(
        "weight_with_unit_via_street_label_not_redacted",
        "squatted 140kg today",
        tags=("incident", "weight_measurement"),
        hits=(("STREET", "140kg", 0.764),),
        exact="squatted 140kg today",
    ),
    _c(
        "pin_marginal_score_weight_not_redacted",
        "my weight is 82kg",
        tags=("incident", "weight_measurement", "pin_override"),
        hits=(("PIN", "82", 0.542),),
        exact="my weight is 82kg",
    ),
    _c(
        "real_name_redacts_alongside_lift_number",
        "tell John Ferris I benched 225",
        tags=("incident", "weight_measurement", "fitness_garbling"),
        redacted=("John Ferris",),
        preserved=("225",),
        hits=(("FULLNAME", "John Ferris", 0.9), ("BUILDINGNUMBER", "225", 0.707)),
    ),
    _c(
        "five_digit_number_mislabeled_buildingnumber_still_redacts",
        "code 90210",
        tags=("incident", "weight_measurement"),
        notes="Belt-and-suspenders: the 4-digit cap on _BARE_MEASUREMENT_RE means "
        "a 5-digit run is never treated as a measurement, so it redacts even alone.",
        redacted=("90210",),
        hits=(("BUILDINGNUMBER", "90210", 0.9),),
    ),
    # -------------------------------------------------------------------
    # INCIDENT: house-number / address privacy
    # -------------------------------------------------------------------
    _c(
        "address_with_house_number_redacts",
        "I live at 82 Baker Street",
        tags=("incident", "house_number_address"),
        redacted=("82 Baker Street",),
        preserved=("I live at",),
        hits=(("BUILDINGNUMBER", "82", 0.8), ("STREET", "Baker Street", 0.9)),
    ),
    _c(
        "european_order_address_redacts",
        "Hauptstrasse 82 is home",
        tags=("incident", "house_number_address"),
        redacted=("Hauptstrasse 82",),
        preserved=("is home",),
        hits=(("STREET", "Hauptstrasse", 0.9), ("BUILDINGNUMBER", "82", 0.8)),
    ),
    _c(
        "alphanumeric_house_number_redacts",
        "ship to 221B Baker Street, London",
        tags=("incident", "house_number_address"),
        notes="'221B' is not purely numeric, so the measurement guard never applies to it.",
        redacted=("221B Baker Street", "London"),
        preserved=("ship to",),
        hits=(
            ("BUILDINGNUMBER", "221B", 0.85),
            ("STREET", "Baker Street", 0.9),
            ("CITY", "London", 0.9),
        ),
    ),
    _c(
        "zip_code_redacts_regardless_of_label",
        "zip is 90210",
        tags=("incident", "house_number_address"),
        redacted=("90210",),
        preserved=("zip is",),
        hits=(("ZIPCODE", "90210", 0.9),),
    ),
    _c(
        "lone_building_number_no_street_unredacted",
        "I'm at number 82",
        tags=("incident", "house_number_address"),
        notes="Accepted trade-off (pinned, not a bug): a bare number with no street/city context is non-identifying.",
        hits=(("BUILDINGNUMBER", "82", 0.9),),
        exact="I'm at number 82",
    ),
    # -------------------------------------------------------------------
    # INCIDENT: PIN score override
    # -------------------------------------------------------------------
    _c(
        "pin_below_override_not_redacted",
        "my PIN is 4821",
        tags=("incident", "pin_override"),
        hits=(("PIN", "4821", 0.6),),
        exact="my PIN is 4821",
    ),
    _c(
        "pin_above_override_redacted_as_password",
        "my PIN is 4821",
        tags=("incident", "pin_override"),
        redacted=("4821",),
        preserved=("my PIN is",),
        hits=(("PIN", "4821", 0.8),),
    ),
    # -------------------------------------------------------------------
    # INCIDENT: fitness-context garbling (exercise vocab mislabeled as PII)
    # -------------------------------------------------------------------
    _c(
        "exercise_phrase_and_rep_scheme_preserved",
        "did Romanian Deadlifts 5x5 at 315 lbs",
        tags=("incident", "fitness_garbling"),
        hits=(("FULLNAME", "Romanian Deadlifts", 0.85), ("STREET", "315 lbs", 0.7)),
        exact="did Romanian Deadlifts 5x5 at 315 lbs",
    ),
    _c(
        "vinyasa_flow_token_preserved",
        "morning vinyasa flow before work",
        tags=("incident", "fitness_garbling"),
        notes="Token-level fitness guard: 'inyasa flow' style partial spans still suppress.",
        hits=(("STREET", "vinyasa flow", 0.72),),
        exact="morning vinyasa flow before work",
    ),
    _c(
        "glute_bridge_march_phrase_preserved",
        "finished with glute bridge marches, 3 sets",
        tags=("incident", "fitness_garbling"),
        hits=(("FULLNAME", "glute bridge marches", 0.8),),
        exact="finished with glute bridge marches, 3 sets",
    ),
    _c(
        "supplement_and_equipment_words_preserved",
        "mixed creatine with the kettlebell session notes",
        tags=("incident", "fitness_garbling"),
        hits=(("LASTNAME", "creatine", 0.6), ("FIRSTNAME", "kettlebell", 0.55)),
        exact="mixed creatine with the kettlebell session notes",
    ),
    _c(
        "pec_deck_flys_partial_span_preserved",
        "pec deck flys felt heavy today",
        tags=("incident", "fitness_garbling"),
        hits=(("STREET", "pec deck flys", 0.7),),
        exact="pec deck flys felt heavy today",
    ),
    _c(
        "imperative_common_word_mark_task_preserved",
        "Mark task complete and email the team.",
        tags=("incident", "fitness_garbling", "common_word_guard"),
        notes="Name-collision stoplist word suppressed ONLY at sentence-initial position.",
        hits=(("FULLNAME", "Mark", 0.6),),
        exact="Mark task complete and email the team.",
    ),
    _c(
        "mark_delgado_at_sentence_start_still_redacts",
        "Mark Delgado sent the invoice.",
        tags=("standard", "common_word_guard"),
        notes="A following non-stoplist token keeps the span even at sentence start.",
        redacted=("Mark Delgado",),
        preserved=("sent the invoice.",),
        hits=(("FULLNAME", "Mark Delgado", 0.85),),
    ),
    _c(
        "max_mid_sentence_still_redacts",
        "We met Max at the gym Saturday.",
        tags=("standard", "common_word_guard"),
        notes="Name-collision stoplist words only suppress in sentence-initial position; "
        "mid-sentence 'Max' is treated as a real name.",
        redacted=("Max",),
        preserved=("We met", "at the gym Saturday."),
        hits=(("FIRSTNAME", "Max", 0.6),),
    ),
    _c(
        "real_name_still_redacts_alongside_rep_scheme",
        "tell Priya Nandan I benched 225",
        tags=("incident", "fitness_garbling"),
        redacted=("Priya Nandan",),
        preserved=("225",),
        hits=(("FULLNAME", "Priya Nandan", 0.9), ("BUILDINGNUMBER", "225", 0.707)),
    ),
    # -------------------------------------------------------------------
    # INCIDENT: Bug A — degenerate stored entity-map rows must not garble
    # -------------------------------------------------------------------
    _c(
        "degenerate_stored_row_does_not_corrupt_message",
        "Rescheduled the meeting to Thursday afternoon.",
        tags=("incident", "bug_a_garbling"),
        seed_map={"[PERSON_1]": "a", "[LOCATION_1]": "_"},
        clean_placeholders=True,
        exact="Rescheduled the meeting to Thursday afternoon.",
        notes="Single-char / punctuation-only stored rows must be skipped by Step 1, not substituted everywhere.",
    ),
    _c(
        "legit_stored_row_survives_alongside_degenerate_rows",
        "Ping Sautai about the new dish tonight.",
        tags=("incident", "bug_a_garbling"),
        seed_map={"[PERSON_1]": "a", "[PERSON_2]": "Sautai", "[LOCATION_1]": "["},
        redacted=("Sautai",),
        preserved=("Ping", "about the new dish tonight."),
        clean_placeholders=True,
    ),
    _c(
        "stored_name_does_not_rewrite_freshly_minted_placeholder_interior",
        "Text Delgado about the CRYPTO_ADDRESS wallet issue.",
        tags=("incident", "bug_a_garbling"),
        notes="A stored fragment matching part of a TYPE token (e.g. containing "
        "'CRYPTO' or a digit) must never rewrite the interior of a placeholder "
        "this same call just emitted.",
        seed_map={"[PERSON_1]": "Delgado"},
        redacted=("Delgado",),
        preserved=("about the", "wallet issue."),
        clean_placeholders=True,
    ),
    # -------------------------------------------------------------------
    # INCIDENT: same-name fusion — contrast case (fusion itself is a
    # multi-turn behavior; see SEQUENCE_CASES below for the actual gap)
    # -------------------------------------------------------------------
    _c(
        "two_different_full_names_never_fuse",
        "Ken Whitfield and Ken Alvarez are both joining the review.",
        tags=("incident", "same_name_fusion"),
        notes="Different full names never collide on canonical_key; this is the "
        "easy, already-working half of the fusion story.",
        redacted=("Ken Whitfield", "Ken Alvarez"),
        hits=(("FULLNAME", "Ken Whitfield", 0.9), ("FULLNAME", "Ken Alvarez", 0.9)),
    ),
    # -------------------------------------------------------------------
    # STANDARD: person names — given/family/nicknames/hyphenated/apostrophe
    # -------------------------------------------------------------------
    _c(
        "full_given_family_name_redacts",
        "Had lunch with Isabella Ferreira yesterday.",
        tags=("standard", "person_names"),
        redacted=("Isabella Ferreira",),
        preserved=("Had lunch with", "yesterday."),
        hits=(("FULLNAME", "Isabella Ferreira", 0.95),),
    ),
    _c(
        "first_name_only_mid_sentence_redacts",
        "Let's ask Priya about the deadline.",
        tags=("standard", "person_names"),
        redacted=("Priya",),
        preserved=("Let's ask", "about the deadline."),
        hits=(("FIRSTNAME", "Priya", 0.65),),
    ),
    _c(
        "nickname_pattern_redacts",
        "Everyone calls him Big Mike at the warehouse.",
        tags=("standard", "person_names"),
        redacted=("Big Mike",),
        preserved=("Everyone calls him", "at the warehouse."),
        hits=(("FULLNAME", "Big Mike", 0.8),),
    ),
    _c(
        "hyphenated_name_redacts_not_treated_as_code",
        "Jean-Luc Fontaine signed off on the design.",
        tags=("standard", "person_names"),
        notes="The code-identifier kebab-case guard requires all-lowercase, so a "
        "capitalized hyphenated name is never mistaken for an identifier.",
        redacted=("Jean-Luc Fontaine",),
        preserved=("signed off on the design.",),
        hits=(("FULLNAME", "Jean-Luc Fontaine", 0.9),),
    ),
    _c(
        "apostrophe_name_redacts",
        "O'Brien approved the budget this morning.",
        tags=("standard", "person_names"),
        redacted=("O'Brien",),
        preserved=("approved the budget this morning.",),
        hits=(("LASTNAME", "O'Brien", 0.8),),
    ),
    _c(
        "honorific_plus_lastname_merges_and_redacts",
        "Dr. Whitfield will see you at three.",
        tags=("standard", "person_names"),
        redacted=("Dr. Whitfield",),
        preserved=("will see you at three.",),
        hits=(("PREFIX", "Dr.", 0.6), ("LASTNAME", "Whitfield", 0.85)),
    ),
    _c(
        "given_middle_family_name_merges_and_redacts",
        "Anna Marie Castellano joined the board.",
        tags=("standard", "person_names"),
        redacted=("Anna Marie Castellano",),
        preserved=("joined the board.",),
        hits=(
            ("FIRSTNAME", "Anna", 0.8),
            ("MIDDLENAME", "Marie", 0.7),
            ("LASTNAME", "Castellano", 0.85),
        ),
    ),
    _c(
        "name_plus_suffix_merges_and_redacts",
        "Contact Robert Kim Jr. about the lease.",
        tags=("standard", "person_names"),
        redacted=("Robert Kim Jr.",),
        preserved=("Contact", "about the lease."),
        hits=(("FULLNAME", "Robert Kim", 0.85), ("SUFFIX", "Jr.", 0.6)),
    ),
    # -------------------------------------------------------------------
    # STANDARD: emails (real Presidio EmailRecognizer, no neural mocking)
    # -------------------------------------------------------------------
    _c(
        "simple_email_redacts",
        "Send the report to alex.rivera@example.com by Friday.",
        tags=("standard", "emails"),
        redacted=("alex.rivera@example.com",),
        preserved=("Send the report to", "by Friday."),
    ),
    _c(
        "plus_addressed_email_redacts",
        "Sign up using sam.oconnor+newsletter@example.org please.",
        tags=("standard", "emails"),
        redacted=("sam.oconnor+newsletter@example.org",),
        preserved=("Sign up using", "please."),
    ),
    _c(
        "subdomain_email_redacts",
        "Escalate to support@mail.example.io if it breaks.",
        tags=("standard", "emails"),
        redacted=("support@mail.example.io",),
        preserved=("Escalate to", "if it breaks."),
    ),
    _c(
        "hyphenated_domain_email_redacts",
        "Reach the vendor at accounts@big-supplier-co.com today.",
        tags=("standard", "emails"),
        redacted=("accounts@big-supplier-co.com",),
        preserved=("Reach the vendor at", "today."),
    ),
    _c(
        "two_emails_numbered_sequentially",
        "CC diego.hernandez@example.com and maria.garcia@example.com on the release notes.",
        tags=("standard", "emails", "multi_entity"),
        redacted=("diego.hernandez@example.com", "maria.garcia@example.com"),
        preserved=("on the release notes.",),
    ),
    # -------------------------------------------------------------------
    # STANDARD: phones, international formats (real Presidio PhoneRecognizer)
    # -------------------------------------------------------------------
    _c(
        "us_phone_parens_redacts",
        "My trainer's cell is (212) 555-0173, text before 8am.",
        tags=("standard", "phones"),
        redacted=("(212) 555-0173",),
        preserved=("My trainer's cell is", "text before 8am."),
    ),
    _c(
        "us_phone_dashed_redacts",
        "My number is 415-555-0188 if the app logs me out.",
        tags=("standard", "phones"),
        redacted=("415-555-0188",),
        preserved=("My number is", "if the app logs me out."),
    ),
    _c(
        "uk_phone_intl_redacts",
        "Ring the front desk on +44 20 7946 0958.",
        tags=("standard", "phones"),
        redacted=("+44 20 7946 0958",),
        preserved=("Ring the front desk on",),
    ),
    _c(
        "jp_phone_intl_redacts",
        "Text +81 90-1234-5678 when the class is confirmed.",
        tags=("standard", "phones", "jp"),
        redacted=("+81 90-1234-5678",),
        preserved=("Text", "when the class is confirmed."),
    ),
    _c(
        "us_phone_plus1_redacts",
        "Call the studio at +1-415-555-0142 to book the class.",
        tags=("standard", "phones"),
        redacted=("+1-415-555-0142",),
        preserved=("Call the studio at", "to book the class."),
    ),
    _c(
        "phone_backstop_ignores_fitness_digit_run",
        "5x5 at 315 today, felt strong.",
        tags=("standard", "phones", "fitness_garbling"),
        notes="libphonenumber VALID-leniency means fitness digit runs never false-positive as phone numbers.",
        exact="5x5 at 315 today, felt strong.",
    ),
    # -------------------------------------------------------------------
    # STANDARD: financial / structured types (credit card, IBAN, account,
    # crypto, ID document, IP address, password) — checksum/shape validated
    # -------------------------------------------------------------------
    _c(
        "credit_card_visa_dashed_redacts",
        "Card number: 4012-8888-8888-1881, expires next year.",
        tags=("standard", "financial"),
        redacted=("4012-8888-8888-1881",),
        hits=(("CREDITCARDNUMBER", "4012-8888-8888-1881", 0.85),),
    ),
    _c(
        "credit_card_mastercard_spaced_redacts",
        "Refund goes back to 5500 0055 5555 5559.",
        tags=("standard", "financial"),
        redacted=("5500 0055 5555 5559",),
        hits=(("CREDITCARDNUMBER", "5500 0055 5555 5559", 0.85),),
    ),
    _c(
        "iban_de_redacts",
        "Wire the deposit to DE89370400440532013000 by Monday.",
        tags=("standard", "financial"),
        redacted=("DE89370400440532013000",),
        preserved=("by Monday.",),
    ),
    _c(
        "iban_gb_redacts",
        "The landlord's IBAN is GB29NWBK60161331926819.",
        tags=("standard", "financial"),
        redacted=("GB29NWBK60161331926819",),
    ),
    _c(
        "account_number_redacts",
        "Reference account number 8834021156 when you call support.",
        tags=("standard", "financial"),
        redacted=("8834021156",),
        preserved=("when you call support.",),
        hits=(("ACCOUNTNUMBER", "8834021156", 0.8),),
    ),
    _c(
        "vehicle_vin_redacts",
        "The VIN is 1HGCM82633A004352 for the rental.",
        tags=("standard", "financial"),
        redacted=("1HGCM82633A004352",),
        preserved=("for the rental.",),
        hits=(("VEHICLEVIN", "1HGCM82633A004352", 0.85),),
    ),
    _c(
        "bitcoin_address_redacts",
        "Send BTC to 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa for the deposit.",
        tags=("standard", "financial"),
        redacted=("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",),
        preserved=("Send BTC to", "for the deposit."),
        hits=(("BITCOINADDRESS", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", 0.9),),
    ),
    _c(
        "ip_address_redacts",
        "The proxy log showed 203.0.113.42 connecting overnight.",
        tags=("standard", "financial"),
        redacted=("203.0.113.42",),
        preserved=("connecting overnight.",),
        hits=(("IP", "203.0.113.42", 0.9),),
    ),
    _c(
        "password_redacts",
        "the wifi password is Sunflower88",
        tags=("standard", "financial"),
        redacted=("Sunflower88",),
        hits=(("PASSWORD", "Sunflower88", 0.85),),
    ),
    _c(
        "credit_card_neural_mislabel_of_prose_word_filtered",
        "our deploy script is called django and it never breaks",
        tags=("standard", "financial", "adversarial"),
        notes="Cites the audited prod false positive: a neural CREDIT_CARD hit "
        "with no Luhn structure ('django') must be filtered by "
        "hygiene.validate_structured, not redacted.",
        hits=(("CREDITCARDNUMBER", "django", 0.72),),
        exact="our deploy script is called django and it never breaks",
    ),
    # -------------------------------------------------------------------
    # STANDARD: locations vs. generic place words
    # -------------------------------------------------------------------
    _c(
        "real_city_redacts",
        "flying into Denver next Tuesday for the summit",
        tags=("standard", "locations"),
        redacted=("Denver",),
        preserved=("flying into", "next Tuesday for the summit"),
        hits=(("CITY", "Denver", 0.85),),
    ),
    _c(
        "generic_place_word_not_detected",
        "let's grab coffee downtown before the show",
        tags=("standard", "locations"),
        exact="let's grab coffee downtown before the show",
    ),
    _c(
        "region_word_preserved_city_redacts",
        "she's originally from the Midwest but living in Austin now",
        tags=("standard", "locations"),
        redacted=("Austin",),
        preserved=("Midwest",),
        hits=(("CITY", "Austin", 0.8),),
    ),
    # -------------------------------------------------------------------
    # STANDARD: organizations (COMPANY_NAME intentionally unmapped — config.py)
    # -------------------------------------------------------------------
    _c(
        "company_name_neural_hit_not_mapped_preserved",
        "I've been working at Meridian Analytics for six years.",
        tags=("standard", "organizations"),
        notes="COMPANY_NAME is deliberately absent from DEBERTA_LABEL_MAP — even a "
        "high-confidence simulated hit on it never reaches _filter_results.",
        hits=(("COMPANY_NAME", "Meridian Analytics", 0.95),),
        exact="I've been working at Meridian Analytics for six years.",
    ),
    _c(
        "jp_company_name_preserved_no_crash",
        "彼はソニー株式会社で働いています。",
        tags=("standard", "organizations", "jp"),
        exact="彼はソニー株式会社で働いています。",
    ),
    # -------------------------------------------------------------------
    # STANDARD: dates-of-birth vs. ordinary dates
    # -------------------------------------------------------------------
    _c(
        "ordinary_journal_date_heading_preserved",
        "# 2026-03-26\n\nGood focus session today.",
        tags=("standard", "dates"),
        notes="DATE is deliberately dropped from DEBERTA_LABEL_MAP (fires on every "
        "yyyy-mm-dd journal heading); no detector emits it, so headings survive.",
        exact="# 2026-03-26\n\nGood focus session today.",
    ),
    _c(
        "date_of_birth_statement_redacts_under_birth_context",
        "My date of birth is March 3, 1990.",
        tags=("standard", "dates"),
        # FIXED (was a KNOWN-GAP sentinel). A raw DATE span now redacts as
        # DATE_OF_BIRTH when a birth-context cue sits beside it, via THREE
        # coordinated changes: (1) redactor._detect_pii promotes a raw DATE hit to
        # DATE_OF_BIRTH under a birth-context cue ("date of birth", "born",
        # "生年月日", …) — a plain calendar date with no cue is still dropped
        # (see plain_calendar_date_non_birth_context_preserved below); (2)
        # DATE_OF_BIRTH is a starter-tier entity in config.py so the promoted span
        # clears the entities gate; and (3) hygiene.validate_structured accepts a
        # date-SHAPED span for DATE_OF_BIRTH (and hygiene.is_junk_span no longer
        # culls an ISO birth date as numeric_datelike), instead of failing closed
        # on an unknown type. This case feeds the raw DATE hit real detection
        # produces, so all three are observed directly.
        hits=(("DATE", "March 3, 1990", 0.9),),
        redacted=("March 3, 1990",),
        preserved=("My date of birth is",),
    ),
    _c(
        "jp_date_of_birth_redacts_under_birth_context",
        "生年月日は1990年3月3日です。",
        tags=("standard", "dates", "jp"),
        notes="FIXED (was a KNOWN-GAP sentinel): the Japanese-format counterpart of "
        "date_of_birth_statement_redacts_under_birth_context. The JP cue '生年月日' "
        "promotes the raw DATE hit to DATE_OF_BIRTH, hygiene.validate_structured "
        "accepts the 年月日 date shape, and the surrounding sentence is preserved.",
        hits=(("DATE", "1990年3月3日", 0.9),),
        redacted=("1990年3月3日",),
        preserved=("生年月日は", "です。"),
    ),
    _c(
        "plain_calendar_date_non_birth_context_preserved",
        "The meeting is on March 3, 1990.",
        tags=("standard", "dates"),
        notes="Regression guard for the DOB fix's context gate: the SAME raw DATE "
        "hit as date_of_birth_statement_redacts_under_birth_context, but with no "
        "birth-context cue nearby, is NOT promoted to DATE_OF_BIRTH — it stays "
        "dropped, exactly as every ordinary calendar date does, so the fix does "
        "not over-redact dates.",
        hits=(("DATE", "March 3, 1990", 0.9),),
        exact="The meeting is on March 3, 1990.",
    ),
    # -------------------------------------------------------------------
    # STANDARD: mixed-language (Japanese names / addresses)
    # -------------------------------------------------------------------
    _c(
        "jp_full_name_redacts",
        "田中太郎、会議に来てください",
        tags=("standard", "jp"),
        notes="Delimited by a full-width comma (、, Unicode punctuation, not "
        "alnum) so snap_to_word_boundaries has a real stop on the right edge — "
        "see jp_unspaced_text_over_redacted_by_word_snap for what happens without one.",
        redacted=("田中太郎",),
        preserved=("会議に来てください",),
        hits=(("FULLNAME", "田中太郎", 0.9),),
    ),
    _c(
        "jp_katakana_transliterated_name_redacts",
        "マイケル・ジョーンズ、明日到着予定です",
        tags=("standard", "jp"),
        redacted=("マイケル・ジョーンズ",),
        preserved=("明日到着予定です",),
        hits=(("FULLNAME", "マイケル・ジョーンズ", 0.9),),
    ),
    _c(
        "jp_address_with_block_number_redacts",
        "住所：東京都渋谷区代々木2-3-15",
        tags=("standard", "jp", "house_number_address"),
        notes="Full-width colon (：) gives the left edge a real stop; the span "
        "runs to end-of-string on the right so there is nothing to over-expand into. "
        "One combined CITY+number hit models a pipeline that already aggregated the "
        "multi-token address (aggregation_strategy='simple'), sidestepping the "
        "separate merge-adjacent-spans path exercised by the ASCII address cases.",
        redacted=("東京都渋谷区代々木2-3-15",),
        preserved=("住所：",),
        hits=(("CITY", "東京都渋谷区代々木2-3-15", 0.9),),
    ),
    _c(
        "jp_postal_code_redacts",
        "郵便番号：150-0041",
        tags=("standard", "jp", "house_number_address"),
        notes="FIXED (was a KNOWN-GAP sentinel). A Japanese postal code (NNN-NNNN) "
        "shares the exact surface shape of a bare numeric range ('18-29') and used "
        "to fullmatch hygiene._BARE_NUM_RANGE_RE's range alternative, so a "
        "correctly-labeled ZIPCODE hit (which collapses to LOCATION) was dropped as "
        "'numeric_datelike' junk and never redacted. hygiene._POSTAL_CODE_RE now "
        "exempts the postal shape from that range guard, so the ZIPCODE redacts. A "
        "US ZIP+4 ('94103-1234') already redacted (its 5-digit side exceeds the "
        "range regex's 4-digit cap). See bare_number_range_still_suppressed_not_"
        "postal for the other direction.",
        redacted=("150-0041",),
        preserved=("郵便番号",),
        hits=(("ZIPCODE", "150-0041", 0.8),),
    ),
    _c(
        "bare_number_range_still_suppressed_not_postal",
        "the trip forecast was 18-29 that week",
        tags=("standard", "jp", "house_number_address"),
        notes="Regression guard paired with jp_postal_code_redacts: a genuine bare "
        "numeric range ('18-29') mislabeled LOCATION must STILL be suppressed by "
        "hygiene._BARE_NUM_RANGE_RE (it is not postal-shaped), so exempting the "
        "postal shape did not re-open the temperature/measurement-range false "
        "positive the range guard was built to prevent.",
        hits=(("CITY", "18-29", 0.8),),
        exact="the trip forecast was 18-29 that week",
    ),
    _c(
        "jp_unspaced_name_redacts_only_the_name",
        "田中太郎さんによろしくお伝えください",
        tags=("standard", "jp"),
        notes="FIXED (was a KNOWN-GAP sentinel). snap_to_word_boundaries used to "
        "expand each edge of a PERSON/LOCATION span over any run of Unicode-alnum "
        "characters; Japanese has no ASCII word breaks, so a short name span with "
        "no adjacent punctuation swallowed the ENTIRE unpunctuated sentence into "
        "one [PERSON_1] (and stored the sentence as a fake name). The snap now "
        "stops at CJK code points (hygiene._is_snap_expandable), so only '田中太郎' "
        "redacts and the honorific + request survive. See "
        "jp_three_char_name_redacts_only_the_name and "
        "jp_two_char_name_redacts_not_dropped_as_degenerate for the shorter "
        "lengths — none may leak.",
        redacted=("田中太郎",),
        preserved=("さんによろしくお伝えください",),
        hits=(("FULLNAME", "田中太郎", 0.85),),
    ),
    _c(
        "jp_three_char_name_redacts_only_the_name",
        "田中太さんによろしくお伝えください",
        tags=("standard", "jp"),
        notes="Length-coverage guard for the snap fix: a 3-character JP name in the "
        "same unspaced sentence redacts ONLY the name, preserving the honorific and "
        "request — the CJK-aware snap holds across 2/3/4-char names, not just the "
        "4-char case.",
        redacted=("田中太",),
        preserved=("さんによろしくお伝えください",),
        hits=(("FULLNAME", "田中太", 0.85),),
    ),
    _c(
        "jp_two_char_name_redacts_not_dropped_as_degenerate",
        "田中、会議に来てください",
        tags=("standard", "jp"),
        notes="FIXED (was a KNOWN-GAP sentinel), and the load-bearing pair to the "
        "snap fix: _is_degenerate_span's 3-char floor is Latin-calibrated and used "
        "to drop a complete 2-character Japanese surname (田中) as 'degenerate'. It "
        "is now CJK-aware (a span carrying a Han/kana char is exempt from the "
        "floor), so a 2-kanji name redacts instead of leaking. Without this, the "
        "snap fix above would flip a 2-char name from over-redacted straight to "
        "LEAKED in cleartext. The full-width comma already isolates the span to "
        "exactly '田中', so this is observed independently of the snap change.",
        redacted=("田中",),
        preserved=("会議に来てください",),
        hits=(("FULLNAME", "田中", 0.85),),
    ),
    _c(
        "mixed_jp_english_sentence_name_redacts",
        "Please contact 佐藤花子 for the Tokyo office details.",
        tags=("standard", "jp"),
        redacted=("佐藤花子",),
        preserved=("Please contact", "for the Tokyo office details."),
        hits=(("FULLNAME", "佐藤花子", 0.88),),
    ),
    # -------------------------------------------------------------------
    # STANDARD: multi-entity sentences
    # -------------------------------------------------------------------
    _c(
        "name_email_phone_all_redact_distinctly",
        "Reach Priya Nandakumar at priya.n@example.com or (415) 555-0134 about the invoice.",
        tags=("standard", "multi_entity"),
        redacted=("Priya Nandakumar", "priya.n@example.com", "(415) 555-0134"),
        preserved=("about the invoice.",),
        hits=(("FULLNAME", "Priya Nandakumar", 0.9),),
    ),
    _c(
        "name_address_card_all_redact_distinctly",
        "Ship the replacement card 4012-8888-8888-1881 to Naomi Fields at 47 Willow Lane, Bristol.",
        tags=("standard", "multi_entity"),
        redacted=("4012-8888-8888-1881", "Naomi Fields", "47 Willow Lane", "Bristol"),
        preserved=("Ship the replacement card", "to", "at"),
        hits=(
            ("FULLNAME", "Naomi Fields", 0.9),
            ("BUILDINGNUMBER", "47", 0.8),
            ("STREET", "Willow Lane", 0.9),
            ("CITY", "Bristol", 0.85),
        ),
    ),
    _c(
        "name_pin_fitness_number_mixed_message",
        "Tell Elena Voss my PIN is 4821, I benched 225 today.",
        tags=("standard", "multi_entity", "weight_measurement", "pin_override"),
        redacted=("Elena Voss", "4821"),
        preserved=("225", "I benched", "today."),
        hits=(
            ("FULLNAME", "Elena Voss", 0.9),
            ("PIN", "4821", 0.8),
            ("BUILDINGNUMBER", "225", 0.707),
        ),
    ),
    _c(
        "two_emails_and_prose_all_correct",
        "Email bob.tanaka@example.com and alice.moreau@example.com about the project.",
        tags=("standard", "multi_entity", "emails"),
        redacted=("bob.tanaka@example.com", "alice.moreau@example.com"),
        preserved=("about the project.",),
    ),
    # -------------------------------------------------------------------
    # STANDARD: empty / whitespace / emoji-only text
    # -------------------------------------------------------------------
    _c(
        "empty_string_unchanged",
        "",
        tags=("standard", "empty_input"),
        exact="",
    ),
    _c(
        "whitespace_only_unchanged",
        "   \n\t  ",
        tags=("standard", "empty_input"),
        exact="   \n\t  ",
    ),
    _c(
        "emoji_only_no_crash",
        "🎉🔥😀🙌",
        tags=("standard", "empty_input"),
        exact="🎉🔥😀🙌",
    ),
    _c(
        "emoji_with_real_email_still_redacts",
        "🎉 email me at alex.rivera@example.com 🔥",
        tags=("standard", "empty_input", "emails"),
        redacted=("alex.rivera@example.com",),
        preserved=("🎉 email me at", "🔥"),
    ),
    # -------------------------------------------------------------------
    # ADVERSARIAL: near-misses — things that LOOK like PII but aren't, and
    # things that don't look like PII but currently redact anyway
    # -------------------------------------------------------------------
    _c(
        "username_label_never_mapped_preserved",
        "my old forum password was hunter2, lol, don't judge",
        tags=("adversarial",),
        notes="USERNAME is deliberately absent from DEBERTA_LABEL_MAP (fires on "
        "'hunter' inside 'password is hunter2' per config.py) — dropped before "
        "reaching _filter_results regardless of confidence.",
        hits=(("USERNAME", "hunter", 0.9),),
        exact="my old forum password was hunter2, lol, don't judge",
    ),
    _c(
        "brand_name_with_honorific_shape_over_redacts",
        "I still make drip coffee in my old Mr. Coffee every morning.",
        tags=("adversarial",),
        notes="Accepted over-redaction, not a test failure: a PREFIX+NOUN-shaped "
        "brand name ('Mr. Coffee') has no product/brand allowlist guard, unlike "
        "fitness vocab or common-word imperatives which DO have dedicated guards — "
        "if the model tags it PERSON it redacts like a real honorific+name. Flagged "
        "for the orchestrator as a narrative gap (a brand-name allowlist doesn't "
        "exist), not encoded as known_gap since the current, over-redacting "
        "behavior IS what this case asserts.",
        redacted=("Mr. Coffee",),
        hits=(("PREFIX", "Mr.", 0.55), ("LASTNAME", "Coffee", 0.6)),
    ),
    _c(
        "sneaker_brand_common_name_over_redacts",
        "My Jordan 1s finally came back from the cobbler looking brand new.",
        tags=("adversarial",),
        notes="Accepted over-redaction, not a test failure: 'Jordan' as a "
        "sneaker-line reference has the same surface form as the common first "
        "name and no brand allowlist exists to tell them apart — consistent with "
        "the existing test_ambiguous_name_handled_contextually acknowledgment that "
        "this class is genuinely ambiguous. Flagged for the orchestrator as a "
        "narrative gap, not encoded as known_gap since the redaction happens "
        "exactly as asserted.",
        redacted=("Jordan",),
        hits=(("FIRSTNAME", "Jordan", 0.6),),
    ),
    _c(
        "topic_reference_place_name_redacted_by_design",
        "She jokes that she has Paris syndrome — always disappointed by the real city.",
        tags=("adversarial",),
        notes="Accepted trade-off, not a bug: the redactor never distinguishes "
        "'talking about a place' from 'disclosing my location' — over-redacting "
        "a topical place mention is judged safer than under-redacting a real one.",
        redacted=("Paris",),
        hits=(("CITY", "Paris", 0.75),),
    ),
    _c(
        "zero_width_obfuscated_name_treated_as_junk",
        "internal note: John​Smith flagged for review",
        tags=("adversarial", "incident"),
        notes="Cites the prod audit's 'invisible' junk class (hygiene.is_junk_span): "
        "a span carrying a zero-width space is treated as machine junk and never "
        "mints, even though a real name happens to be underneath the obfuscation.",
        hits=(("FULLNAME", "John​Smith", 0.8),),
        exact="internal note: John​Smith flagged for review",
    ),
    _c(
        "markdown_table_separator_treated_as_structure_junk",
        "| Name | Score |\n|------|-------|\nBudget review passed.",
        tags=("adversarial", "incident"),
        notes="Cites the prod audit's dominant junk source: agent-authored markdown "
        "structure mislabeled PERSON must never mint (hygiene.is_junk_span 'structure').",
        hits=(("FULLNAME", "| Name | Score |", 0.7),),
        exact="| Name | Score |\n|------|-------|\nBudget review passed.",
    ),
    _c(
        "snake_case_identifier_treated_as_code_junk",
        "the crash trace points at user_profile_v2 again",
        tags=("adversarial",),
        notes="A single-token snake_case identifier mislabeled PERSON is caught by "
        "hygiene's code-identifier guard, not redacted as a name.",
        hits=(("FULLNAME", "user_profile_v2", 0.65),),
        exact="the crash trace points at user_profile_v2 again",
    ),
    _c(
        "dotted_config_path_treated_as_code_junk",
        "double check config.settings.production before deploying",
        tags=("adversarial",),
        hits=(("LASTNAME", "config.settings.production", 0.6),),
        exact="double check config.settings.production before deploying",
    ),
    _c(
        "lowercase_kebab_identifier_treated_as_code_junk",
        "toggle feature-flag-rollout before the demo",
        tags=("adversarial",),
        notes="All-lowercase kebab-case is caught as a code identifier; contrast "
        "with hyphenated_name_redacts_not_treated_as_code where mixed case survives.",
        hits=(("STREET", "feature-flag-rollout", 0.55),),
        exact="toggle feature-flag-rollout before the demo",
    ),
    _c(
        "temperature_range_not_treated_as_account",
        "forecast says 18-29°C for the trip",
        tags=("adversarial",),
        notes="Degree signs / en-dashes fall outside the secret-run charset, so a "
        "neural ACCOUNT mislabel on a temperature range is filtered, not redacted.",
        hits=(("ACCOUNTNUMBER", "18-29°C", 0.55),),
        exact="forecast says 18-29°C for the trip",
    ),
]


# ---------------------------------------------------------------------------
# Sequence cases: same-name fusion can only be observed across multiple
# messages against the SAME tenant's persistent pii_entity_map, so these
# live outside the single-text CASES list above.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SequenceEvalCase:
    id: str
    tags: tuple[str, ...]
    turns: tuple[str, ...]
    turn_raw_hits: tuple[tuple[RawHit, ...], ...]
    assertion: str  # "same_placeholder" | "distinct_placeholders"
    known_gap: str | None = None
    notes: str = ""
    is_synthetic: bool = True


def _seq(
    id: str,
    turns: tuple[str, ...],
    turn_hits: tuple[tuple[tuple[str, str, float], ...], ...],
    *,
    tags: tuple[str, ...],
    assertion: str,
    known_gap: str | None = None,
    notes: str = "",
) -> SequenceEvalCase:
    return SequenceEvalCase(
        id=id,
        tags=tags,
        turns=turns,
        turn_raw_hits=tuple(tuple(RawHit(*h) for h in hits) for hits in turn_hits),
        assertion=assertion,
        known_gap=known_gap,
        notes=notes,
    )


SEQUENCE_CASES: list[SequenceEvalCase] = [
    _seq(
        "same_name_fusion_two_different_people",
        (
            "Ping John Carter about the roadmap review.",
            "John Carter picked up my kid from school today.",
        ),
        (
            (("FULLNAME", "John Carter", 0.9),),
            (("FULLNAME", "John Carter", 0.9),),
        ),
        tags=("incident", "same_name_fusion"),
        assertion="distinct_placeholders",
        known_gap="KNOWN-GAP: canonical_key() is an exact casefold+strip match on "
        "the surface string only (apps/pii/entity_registry.py) — two different "
        "real people who happen to share an identical full name silently and "
        "permanently fuse onto the same placeholder across messages. Verified and "
        "documented 2026-07-06; structural fixes (collision-visibility signal, "
        "thread-scoped sessions, entity-linking arbiter pass, or a stable people "
        "registry) are ranked but NOT implemented pending an owner decision. "
        "FLIPS WHEN: any behavior-level disambiguation lands anywhere in the "
        "redaction path (canonical_key gains context-awareness, Step 1's "
        "known-entity regex pass or the row-locked mint-reuse branch in "
        "redactor._redact_user_message consults something beyond an exact "
        "casefolded string match, etc. — turn 2 here is actually caught by "
        "Step 1's literal substitution of the already-known name, not the "
        "mint-reuse branch, since 'John Carter' is already in the tenant map "
        "by then) — this case already feeds two real FULLNAME hits for the "
        "identical surface string across two turns against the SAME tenant, "
        "so any such fix is observed directly as distinct placeholders.",
    ),
    _seq(
        "same_person_repeat_mention_intentionally_collapses",
        (
            "Marcus Delgado sent over the contract draft.",
            "Following up with Marcus Delgado again tomorrow.",
        ),
        (
            (("FULLNAME", "Marcus Delgado", 0.9),),
            (("FULLNAME", "Marcus Delgado", 0.9),),
        ),
        tags=("standard", "same_name_fusion"),
        assertion="same_placeholder",
        notes="This is the INTENDED behavior, not a bug: the same full name across "
        "two messages is treated as the same person, which is correct the "
        "overwhelming majority of the time. Contrast with "
        "same_name_fusion_two_different_people above, where that assumption breaks.",
    ),
    _seq(
        "fusion_accidentally_avoided_by_divergent_surface_form",
        (
            "Ken Tanaka is joining the call.",
            "Ken said he'd be running late tonight.",
        ),
        (
            (("FULLNAME", "Ken Tanaka", 0.9),),
            (("FIRSTNAME", "Ken", 0.6),),
        ),
        tags=("incident", "same_name_fusion"),
        assertion="distinct_placeholders",
        notes="The ONLY protection that exists today against fusion is accidental: "
        "'Ken Tanaka' and a bare 'Ken' have different canonical keys, so two "
        "mentions of what may be entirely different people happen to land on "
        "separate placeholders purely because the surface strings differ. This is "
        "not a designed safeguard — a second full mention of 'Ken Tanaka' for a "
        "DIFFERENT Ken Tanaka would still fuse (see the known-gap case above).",
    ),
]


# ---------------------------------------------------------------------------
# Known-entity reuse-path cases — folded in from PR #1144 (a sibling attempt
# at this same corpus that landed on the shared branch in parallel; superseded
# by this module, but its coverage of the REUSE-ONLY entry points —
# redact_known_entities and RedactionSession — is real and worth keeping,
# since CASES/SEQUENCE_CASES above only exercise redact_user_message).
# These are pure-function, DB-free: redact_known_entities only reads
# pii_entity_map/pii_denylist off the tenant, so a SimpleNamespace stub is
# enough (mirrors test_redact_known_entities.py's existing convention).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownEntityEvalCase:
    id: str
    tags: tuple[str, ...]
    entity_map: dict[str, Any]
    denylist: dict[str, Any]
    text: str
    expected_redacted: tuple[str, ...] = ()
    expected_preserved: tuple[str, ...] = ()
    is_synthetic: bool = True


def _ke(
    id: str,
    *,
    tags: tuple[str, ...],
    entity_map: dict[str, Any],
    denylist: dict[str, Any],
    text: str,
    redacted: tuple[str, ...] = (),
    preserved: tuple[str, ...] = (),
) -> KnownEntityEvalCase:
    return KnownEntityEvalCase(
        id=id,
        tags=tags,
        entity_map=entity_map,
        denylist=denylist,
        text=text,
        expected_redacted=redacted,
        expected_preserved=preserved,
    )


KNOWN_ENTITY_CASES: list[KnownEntityEvalCase] = [
    _ke(
        "case_insensitive_known_name_all_variants",
        tags=("standard", "known_entity_reuse"),
        entity_map={"[PERSON_1]": {"name": "Devon Okafor"}},
        denylist={},
        text="Devon Okafor stopped by, so did devon okafor, and DEVON OKAFOR again",
        redacted=("Devon Okafor", "devon okafor", "DEVON OKAFOR"),
        preserved=("stopped by", "again"),
    ),
    _ke(
        "denylisted_brand_name_stops_redacting",
        tags=("adversarial", "known_entity_reuse", "denylist"),
        entity_map={"[PERSON_1]": {"name": "Nimbus Kitchen"}},
        denylist={"nimbus kitchen": {"reason": "false_positive"}},
        text="Nimbus Kitchen posted a new recipe today",
        preserved=("Nimbus Kitchen", "posted a new recipe today"),
    ),
    _ke(
        "denylisted_common_word_stops_redacting",
        tags=("adversarial", "known_entity_reuse", "denylist"),
        entity_map={"[PERSON_1]": {"name": "goal"}},
        denylist={"goal": {"reason": "arbiter"}},
        text="my goal this week is consistency",
        preserved=("goal", "consistency"),
    ),
    _ke(
        "denylist_is_per_entry_not_per_map",
        tags=("standard", "known_entity_reuse", "denylist"),
        entity_map={
            "[PERSON_1]": {"name": "Nimbus Kitchen"},
            "[PERSON_2]": {"name": "Amara Whitfield"},
        },
        denylist={"nimbus kitchen": {"reason": "false_positive"}},
        text="Nimbus Kitchen told Amara Whitfield about the plan",
        redacted=("Amara Whitfield",),
        preserved=("Nimbus Kitchen", "about the plan"),
    ),
]
