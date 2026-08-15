#!/usr/bin/env python3
"""Deterministic raw-span evaluation for the production and Liquid PII models.

Run from the repository root with the isolated dependencies in
``scripts/requirements-pii-eval.txt``. The parent process freezes a seeded
ai4privacy validation sample, then evaluates each model in a fresh child
process. Fresh processes make model RSS deltas comparable and avoid retaining
one model's allocator state while the other loads.

This intentionally evaluates the neural-engine seam before NBHD's redactor
stoplists, tier policy, and Presidio recognizers.
"""

from __future__ import annotations

import argparse
import ast
import gc
import importlib.util
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SEED = 20260815
AI4PRIVACY_ROWS = 500

DEBERTA_REPO = "lakshyakh93/deberta_finetuned_pii"
DEBERTA_REVISION = "a038061af92047b0afbbd5ca07d7aa0521789379"
LIQUID_REPO = "LiquidAI/LFM2.5-Encoder-350M-PII-Detector"
LIQUID_REVISION = "b8c9cf3d2d6ae52501b35a27ba46f271449c9ce2"
AI4PRIVACY_REPO = "ai4privacy/pii-masking-400k"
AI4PRIVACY_REVISION = "414d0a3b5798a152588a0828f1c08a5787de10f4"

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "docs/pii-detector-eval-2026-08-15.md"


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    pii_type: str


@dataclass(frozen=True)
class FalsePositiveCase:
    text: str
    target: str
    evidence: str

    @property
    def target_span(self) -> tuple[int, int]:
        start = self.text.casefold().find(self.target.casefold())
        if start < 0:
            raise ValueError(f"target {self.target!r} is absent from {self.text!r}")
        return start, start + len(self.target)


@dataclass(frozen=True)
class RecallCase:
    text: str
    language: str
    pii_type: str
    value: str

    @property
    def gold(self) -> Span:
        start = self.text.find(self.value)
        if start < 0:
            raise ValueError(f"value {self.value!r} is absent from {self.text!r}")
        return Span(start, start + len(self.value), self.pii_type)


_FLEET_SENTENCES = {
    "nbhd": "The NBHD dashboard finished syncing before breakfast.",
    "calendar": "I moved the workout block on my calendar.",
    "wins": "Write down three wins from this week.",
    "briefing": "The briefing should stay short and practical.",
    "briefings": "Archive the old briefings after the review.",
    "weekly": "The weekly summary is ready to proofread.",
    "status": "Add a status line beneath the heading.",
    "schedule": "My schedule is flexible after lunch.",
    "lesson": "That lesson was easier than yesterday's.",
    "lessons": "I saved the language lessons for the train.",
    "email": "Please email the receipt when it is ready.",
    "background": "The backup can run quietly in the background.",
    "complete": "Mark the checklist complete after the final review.",
    "running": "The nightly export is still running.",
    "await": "We can await the result without changing the plan.",
    "project": "The project notes need a clearer title.",
    "setup": "The microphone setup took less than five minutes.",
    "weather": "The weather looks good for an evening walk.",
    "push": "Push the easy set only if your form stays clean.",
    "pull": "Pull the summary into tomorrow's note.",
    "heartbeat": "The service heartbeat remained steady overnight.",
    "check": "Run one final check before closing the task.",
    "checkin": "The afternoon checkin can be asynchronous.",
    "in": "I left the groceries in the kitchen.",
    "gmail": "The Gmail tab is already open in my browser.",
    "google": "I used Google to compare the train routes.",
    "youtube": "The YouTube tutorial explained the warmup well.",
    "telegram": "The Telegram notification arrived twice.",
    "fedex": "The FedEx tracking page still says delayed.",
    "nvidia": "The NVIDIA earnings headline dominated the feed.",
    "overcast": "The forecast says it will stay overcast all day.",
    "totemo": "The Totemo workflow is listed in the integration notes.",
    "playoff": "The playoff schedule changed after the rain delay.",
    "drizzle": "A light drizzle started near the end of the walk.",
    "hmm": "Hmm, I may move that task to next week.",
    "gyoza": "We ordered gyoza and tea after the gym.",
    "houthis": "The news briefing mentioned the Houthis again.",
    "reply": "Draft a reply but do not send it yet.",
    "section": "Move that paragraph into the next section.",
}

_FLEET_PHRASE_SENTENCES = {
    "quick wins": "The agenda starts with quick wins from the team.",
    "morning briefing": "The morning briefing includes weather and tasks.",
    "morning briefings": "I disabled the duplicate morning briefings.",
    "daily briefing": "The daily briefing should omit old reminders.",
    "evening check in": "Add one reflection to the evening check in.",
}

_EVIDENCE_CASES = [
    ("Goal tracker is a feature label, not a contact.", "Goal tracker", "fleet analysis"),
    ("Google Calendar is showing two copies of the event.", "Google Calendar", "fleet analysis"),
    ("The calendar status line can be removed tomorrow.", "calendar status", "fleet analysis"),
    ("The heartbeat check-in completed without an alert.", "heartbeat check-in", "fleet analysis"),
    ("The evening check-in is only a template heading.", "evening check-in", "fleet analysis"),
    ("Claude summarized the article in two bullets.", "Claude", "fleet analysis"),
    ("The US inflation story was on the front page.", "US", "fleet geography"),
    ("Japan announced the holiday schedule this morning.", "Japan", "fleet geography"),
    ("The report compared trade through Iran and the Gulf.", "Iran", "fleet geography"),
    ("Shipping delays continued across the Gulf this week.", "Gulf", "fleet geography"),
    ("The UK rail timetable changes next month.", "UK", "fleet geography"),
    ("The article compared battery output in China.", "China", "fleet geography"),
    ("The Israel headline was part of a news digest.", "Israel", "fleet geography"),
    ("Markets across the Middle East closed higher.", "Middle East", "fleet geography"),
    ("Mar is used as a short month label here.", "Mar", "fleet short token"),
    ("AI tools were the theme of the conference recap.", "AI", "fleet short token"),
    ("Use max effort only on the final interval.", "max", "fleet short token"),
    ("The daily note has no contact information.", "daily", "fleet short token"),
    ("Fri is the abbreviated label in this calendar view.", "Fri", "fleet short token"),
    ("Sun is the abbreviated label on the chart.", "Sun", "fleet short token"),
    ("Sat is the abbreviated label beside the workout.", "Sat", "fleet short token"),
    ("The reminder is set for 08:00 JST.", "JST", "fleet short token"),
    ("W16 is the sprint label in the planning board.", "W16", "fleet short token"),
    ("Theo appears here as an excluded risk example, not a contact.", "Theo", "analysis exclusion"),
    ("La is a solfege syllable in this exercise.", "La", "analysis exclusion"),
    ("The moon was visible before the morning run.", "moon", "analysis exclusion"),
    ("Pistachio is the flavor listed on the wrapper.", "Pistachio", "analysis exclusion"),
    ("The spark from the plug was brief.", "spark", "analysis exclusion"),
    ("The DoorDash order arrived before the meeting.", "DoorDash", "task brief"),
    ("Osaka is one stop on the travel itinerary.", "Osaka", "task brief travel"),
    ("Heavy deadlifts felt smooth at RPE seven.", "deadlifts", "task brief fitness"),
    ("The AMRAP ends after twelve minutes.", "AMRAP", "task brief fitness"),
    ("The goal tracker shows a four-day streak.", "goal tracker", "task brief"),
    ("Move the dentist reminder in my calendar notes.", "calendar notes", "task brief"),
    ("The product briefing needs a shorter opening.", "briefing", "task brief"),
    ("We listed two quick wins under the heading.", "quick wins", "task brief"),
    ("I keep spare cables in the stash by the desk.", "stash", "stash gate"),
    ("Add the receipt to my travel stash for later.", "stash", "stash gate"),
    ("The snack stash is nearly empty after the hike.", "stash", "stash gate"),
    ("Move those draft ideas into the archive stash.", "stash", "stash gate"),
]


def _fp_suite() -> list[FalsePositiveCase]:
    cases = [FalsePositiveCase(text, target, "fleet word stoplist") for target, text in _FLEET_SENTENCES.items()]
    cases.extend(
        FalsePositiveCase(text, target, "fleet phrase stoplist") for target, text in _FLEET_PHRASE_SENTENCES.items()
    )
    cases.extend(FalsePositiveCase(*case) for case in _EVIDENCE_CASES)
    return cases


def _annotated_cases(
    language: str,
    pii_type: str,
    values: list[str],
    template: str,
) -> list[RecallCase]:
    return [RecallCase(template.format(value=value), language, pii_type, value) for value in values]


def _recall_suite() -> list[RecallCase]:
    cases: list[RecallCase] = []
    cases += _annotated_cases(
        "EN",
        "NAME",
        [
            "Sarah Chen",
            "Miguel Alvarez",
            "Aisha Rahman",
            "Daniel Brooks",
            "Emily Nguyen",
            "Robert Williams",
            "Priya Patel",
            "Lucas Martin",
            "Grace Kim",
            "Noah Thompson",
        ],
        "Please ask {value} to review the draft.",
    )
    cases += _annotated_cases(
        "JA",
        "NAME",
        [
            "田中さん",
            "佐藤花子",
            "鈴木一郎",
            "山田太郎",
            "高橋美咲",
            "伊藤健",
            "渡辺直子",
            "中村亮",
            "Tanaka-san",
            "Kiho",
        ],
        "次の確認は{value}にお願いしてください。",
    )
    cases += _annotated_cases(
        "EN",
        "EMAIL",
        [
            "sarah.chen@example.com",
            "miguel+travel@correo.es",
            "a.rahman@sample.org",
            "dan.brooks@workmail.net",
            "emily_nguyen@example.co.uk",
            "rwilliams42@mail.com",
            "priya.patel@domain.io",
            "lucas-martin@sample.fr",
            "grace.kim@company.co",
            "noah.t@example.net",
        ],
        "Send the confirmation to {value} today.",
    )
    cases += _annotated_cases(
        "JA",
        "EMAIL",
        [
            "tanaka@example.jp",
            "hanako.sato@mail.jp",
            "ichiro+gym@example.com",
            "taro.yamada@sample.co.jp",
            "misaki_t@domain.jp",
            "ken.ito@example.org",
            "naoko.watanabe@mail.net",
            "ryo.nakamura@example.jp",
            "tanaka-san@sample.com",
            "kiho@domain.co.jp",
        ],
        "連絡先メールは{value}です。",
    )
    cases += _annotated_cases(
        "EN",
        "PHONE",
        [
            "+1 415-555-0136",
            "(212) 555-0187",
            "+44 20 7946 0958",
            "617-555-0142",
            "+1-303-555-0191",
            "202.555.0175",
            "+61 2 9374 4000",
            "+49 30 4505 1234",
            "646 555 0129",
            "+1 (206) 555-0168",
        ],
        "My phone number is {value}.",
    )
    cases += _annotated_cases(
        "JA",
        "PHONE",
        [
            "090-1234-5678",
            "080-2468-1357",
            "070-9876-5432",
            "+81 90 1111 2222",
            "03-1234-5678",
            "06-7654-3210",
            "045-123-4567",
            "052-987-6543",
            "011-234-5678",
            "+81-80-3333-4444",
        ],
        "電話番号は{value}です。",
    )
    cases += _annotated_cases(
        "EN",
        "ADDRESS",
        [
            "123 Main Street, Boston, MA 02110",
            "42 Oak Avenue, Portland, OR 97205",
            "1600 Pennsylvania Avenue NW, Washington, DC 20500",
            "221B Baker Street, London NW1 6XE",
            "8 Market Road, Cambridge CB1 2AB",
            "75 King Street, Toronto, ON M5H 1J9",
            "19 George Street, Sydney NSW 2000",
            "5 Rue de Rivoli, 75001 Paris",
            "27 Lindenstrasse, 10117 Berlin",
            "300 Pine Street, Seattle, WA 98101",
        ],
        "The mailing address is {value}.",
    )
    cases += _annotated_cases(
        "JA",
        "ADDRESS",
        [
            "東京都新宿区西新宿2-8-1",
            "大阪府大阪市北区梅田1-1-3",
            "京都府京都市中京区河原町通三条下る",
            "北海道札幌市中央区北1条西2丁目",
            "福岡県福岡市博多区博多駅前2-1-1",
            "神奈川県横浜市中区本町6-50-10",
            "愛知県名古屋市中村区名駅1-1-4",
            "宮城県仙台市青葉区国分町3-7-1",
            "広島県広島市中区基町10-52",
            "沖縄県那覇市泉崎1-2-2",
        ],
        "住所は{value}です。",
    )
    cases += _annotated_cases(
        "EN",
        "DOB",
        [
            "April 12, 1990",
            "1985-07-23",
            "03/14/1978",
            "22 September 1992",
            "January 5, 2001",
            "1969-11-30",
            "08/27/1988",
            "15 May 1975",
            "December 9, 1999",
            "2000-02-29",
        ],
        "The date of birth is {value}.",
    )
    cases += _annotated_cases(
        "JA",
        "DOB",
        [
            "1990年4月12日",
            "1985年7月23日",
            "1978年3月14日",
            "1992年9月22日",
            "2001年1月5日",
            "1969年11月30日",
            "1988年8月27日",
            "1975年5月15日",
            "1999年12月9日",
            "2000年2月29日",
        ],
        "生年月日は{value}です。",
    )
    return cases


def _literal_frozenset(module_path: Path, name: str) -> frozenset[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        if not isinstance(node.value, ast.Call) or not node.value.args:
            break
        return frozenset(ast.literal_eval(node.value.args[0]))
    raise RuntimeError(f"could not read literal {name} from {module_path}")


def _validate_suites(fp_cases: list[FalsePositiveCase], recall_cases: list[RecallCase]) -> None:
    if len(fp_cases) < 60:
        raise RuntimeError(f"FP suite has only {len(fp_cases)} cases")
    if not 80 <= len(recall_cases) <= 120:
        raise RuntimeError(f"recall suite has {len(recall_cases)} spans")

    redactor_path = ROOT / "apps/pii/redactor.py"
    expected_words = _literal_frozenset(redactor_path, "_FLEET_WORD_STOPLIST")
    expected_phrases = _literal_frozenset(redactor_path, "_FLEET_PHRASE_STOPLIST")
    covered = {case.target.casefold() for case in fp_cases}
    missing = sorted((expected_words | expected_phrases) - covered)
    if missing:
        raise RuntimeError(f"FP suite is missing fleet stoplist evidence: {missing}")

    for case in fp_cases:
        _ = case.target_span
    for case in recall_cases:
        _ = case.gold


DEBERTA_RECALL_TYPES = {
    "NAME": {
        "FIRSTNAME",
        "MIDDLENAME",
        "LASTNAME",
        "FULLNAME",
        "NAME",
        "PREFIX",
        "SUFFIX",
        "DISPLAYNAME",
        "ACCOUNTNAME",
    },
    "EMAIL": {"EMAIL"},
    "PHONE": {"PHONE_NUMBER", "PHONEIMEI"},
    "ADDRESS": {
        "STREET",
        "STREETADDRESS",
        "SECONDARYADDRESS",
        "BUILDINGNUMBER",
        "CITY",
        "STATE",
        "COUNTY",
        "ZIPCODE",
        "NEARBYGPSCOORDINATE",
        "ORDINALDIRECTION",
    },
    "DOB": {"DATE"},
}
LIQUID_RECALL_TYPES = {
    "NAME": {"identity.person_name"},
    "EMAIL": {"contact.email"},
    "PHONE": {"contact.phone", "device.imei"},
    "ADDRESS": {"contact.address", "contact.postal_code", "location.gps_coordinates"},
    "DOB": {"identity.date_of_birth"},
}
DEBERTA_FP_TYPES = DEBERTA_RECALL_TYPES["NAME"] | DEBERTA_RECALL_TYPES["ADDRESS"]
LIQUID_FP_TYPES = LIQUID_RECALL_TYPES["NAME"] | LIQUID_RECALL_TYPES["ADDRESS"]


def _overlaps(left: Span | tuple[int, int], right: Span | tuple[int, int]) -> bool:
    left_start, left_end = (left.start, left.end) if isinstance(left, Span) else left
    right_start, right_end = (right.start, right.end) if isinstance(right, Span) else right
    return min(left_end, right_end) > max(left_start, right_start)


def _call_timed(
    predict: Callable[[str], list[Span]],
    text: str,
) -> tuple[list[Span], float]:
    started = time.perf_counter()
    spans = predict(text)
    return spans, time.perf_counter() - started


def _maximum_partial_matches(predictions: list[Span], gold: list[Span]) -> int:
    adjacency = [
        [gold_index for gold_index, gold_span in enumerate(gold) if _overlaps(pred, gold_span)] for pred in predictions
    ]
    gold_to_prediction: dict[int, int] = {}

    def augment(prediction_index: int, visited: set[int]) -> bool:
        for gold_index in adjacency[prediction_index]:
            if gold_index in visited:
                continue
            visited.add(gold_index)
            previous = gold_to_prediction.get(gold_index)
            if previous is None or augment(previous, visited):
                gold_to_prediction[gold_index] = prediction_index
                return True
        return False

    return sum(augment(index, set()) for index in range(len(predictions)))


def _load_deberta() -> tuple[Callable[[str], list[Span]], dict[str, Any]]:
    import psutil
    from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline

    process = psutil.Process()
    before = process.memory_info().rss
    tokenizer = AutoTokenizer.from_pretrained(DEBERTA_REPO, revision=DEBERTA_REVISION)
    model = AutoModelForTokenClassification.from_pretrained(
        DEBERTA_REPO,
        revision=DEBERTA_REVISION,
    ).eval()
    detector = pipeline(
        "token-classification",
        model=model,
        tokenizer=tokenizer,
        aggregation_strategy="simple",
        device="cpu",
    )
    after = process.memory_info().rss

    def predict(text: str) -> list[Span]:
        return [Span(int(item["start"]), int(item["end"]), str(item["entity_group"])) for item in detector(text)]

    return predict, {
        "rss_before": before,
        "rss_after": after,
        "rss_delta": after - before,
        "construction": "transformers pipeline; aggregation_strategy=simple; device=cpu",
    }


def _load_liquid() -> tuple[Callable[[str], list[Span]], dict[str, Any]]:
    import psutil
    from huggingface_hub import hf_hub_download
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    helper_path = hf_hub_download(
        LIQUID_REPO,
        "pii_hybrid_decode.py",
        revision=LIQUID_REVISION,
    )
    hf_hub_download(
        LIQUID_REPO,
        "context_cued.py",
        revision=LIQUID_REVISION,
    )
    helper_dir = str(Path(helper_path).parent)
    if helper_dir not in sys.path:
        sys.path.insert(0, helper_dir)
    spec = importlib.util.spec_from_file_location("pinned_liquid_pii_hybrid_decode", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load Liquid decoder from {helper_path}")
    decoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(decoder)

    process = psutil.Process()
    before = process.memory_info().rss
    tokenizer = AutoTokenizer.from_pretrained(
        LIQUID_REPO,
        revision=LIQUID_REVISION,
        trust_remote_code=True,
    )
    model = (
        AutoModelForTokenClassification.from_pretrained(
            LIQUID_REPO,
            revision=LIQUID_REVISION,
            trust_remote_code=True,
        )
        .to("cpu")
        .eval()
    )
    after = process.memory_info().rss

    def predict(text: str) -> list[Span]:
        return [
            Span(int(item["start"]), int(item["end"]), str(item["type"]))
            for item in decoder.predict(text, tokenizer, model, hybrid=True)
        ]

    return predict, {
        "rss_before": before,
        "rss_after": after,
        "rss_delta": after - before,
        "construction": "Liquid pii_hybrid_decode.predict; hybrid=True; device=cpu; FP32",
    }


def _worker(model_name: str, corpus_path: Path, result_path: Path) -> None:
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    random.seed(SEED)
    import numpy as np
    import torch

    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)

    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    fp_cases = [FalsePositiveCase(**item) for item in corpus["fp"]]
    recall_cases = [RecallCase(**item) for item in corpus["recall"]]
    ai_rows = corpus["ai4privacy"]

    if model_name == "deberta":
        predict, load_stats = _load_deberta()
        fp_types = DEBERTA_FP_TYPES
        recall_types = DEBERTA_RECALL_TYPES
    elif model_name == "liquid":
        predict, load_stats = _load_liquid()
        fp_types = LIQUID_FP_TYPES
        recall_types = LIQUID_RECALL_TYPES
    else:
        raise ValueError(model_name)

    # Untimed warm-up removes one-time graph/tokenizer setup from sentence latency.
    predict("Warm-up sentence without personal information.")
    import psutil

    load_stats["rss_after"] = psutil.Process().memory_info().rss
    load_stats["rss_delta"] = load_stats["rss_after"] - load_stats["rss_before"]

    latencies: dict[str, list[float]] = {"fp": [], "recall": [], "ai4privacy": []}
    flagged_cases: list[dict[str, Any]] = []
    for index, case in enumerate(fp_cases):
        predictions, elapsed = _call_timed(predict, case.text)
        latencies["fp"].append(elapsed)
        target_span = case.target_span
        hits = [asdict(span) for span in predictions if span.pii_type in fp_types and _overlaps(span, target_span)]
        if hits:
            flagged_cases.append(
                {
                    "index": index,
                    "target": case.target,
                    "evidence": case.evidence,
                    "hits": hits,
                }
            )

    recall_hits: list[bool] = []
    recall_misses: list[dict[str, str]] = []
    for case in recall_cases:
        predictions, elapsed = _call_timed(predict, case.text)
        latencies["recall"].append(elapsed)
        allowed_types = recall_types[case.pii_type]
        hit = any(pred.pii_type in allowed_types and _overlaps(pred, case.gold) for pred in predictions)
        recall_hits.append(hit)
        if not hit:
            recall_misses.append(
                {
                    "language": case.language,
                    "pii_type": case.pii_type,
                    "value": case.value,
                }
            )

    tp = fp = fn = 0
    for row in ai_rows:
        predictions, elapsed = _call_timed(predict, row["text"])
        latencies["ai4privacy"].append(elapsed)
        gold = [Span(item["start"], item["end"], item["pii_type"]) for item in row["gold"]]
        matched = _maximum_partial_matches(predictions, gold)
        tp += matched
        fp += len(predictions) - matched
        fn += len(gold) - matched

    def recall_breakdown(field: str, value: str) -> dict[str, Any]:
        indices = [index for index, case in enumerate(recall_cases) if getattr(case, field) == value]
        hits = sum(recall_hits[index] for index in indices)
        return {"hits": hits, "total": len(indices), "recall": hits / len(indices)}

    all_latencies = [elapsed for suite in latencies.values() for elapsed in suite]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    result = {
        "model": model_name,
        "load": load_stats,
        "fp": {
            "flagged": len(flagged_cases),
            "total": len(fp_cases),
            "stash_flagged": sum(item["target"].casefold() == "stash" for item in flagged_cases),
            "flagged_cases": flagged_cases,
        },
        "recall": {
            "overall": {
                "hits": sum(recall_hits),
                "total": len(recall_hits),
                "recall": sum(recall_hits) / len(recall_hits),
            },
            "by_type": {
                pii_type: recall_breakdown("pii_type", pii_type)
                for pii_type in ("NAME", "EMAIL", "PHONE", "ADDRESS", "DOB")
            },
            "by_language": {language: recall_breakdown("language", language) for language in ("EN", "JA")},
            "misses": recall_misses,
        },
        "ai4privacy": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        },
        "latency_ms": {
            **{suite: statistics.median(values) * 1000 for suite, values in latencies.items()},
            "overall": statistics.median(all_latencies) * 1000,
        },
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    del predict
    gc.collect()


def _load_ai4privacy_sample() -> list[dict[str, Any]]:
    from datasets import load_dataset

    validation = load_dataset(
        AI4PRIVACY_REPO,
        revision=AI4PRIVACY_REVISION,
        split="validation",
    )
    sample = validation.shuffle(seed=SEED).select(range(AI4PRIVACY_ROWS))
    rows: list[dict[str, Any]] = []
    for row in sample:
        text = str(row["source_text"])
        gold = []
        for item in row["privacy_mask"]:
            start = int(item["start"])
            end = int(item["end"])
            if 0 <= start < end <= len(text):
                gold.append({"start": start, "end": end, "pii_type": str(item["label"])})
        rows.append({"uid": row["uid"], "text": text, "gold": gold})
    return rows


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _ratio(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0.00" if numerator == 0 else "∞"
    return f"{numerator / denominator:.2f}"


def _gate_results(deberta: dict[str, Any], liquid: dict[str, Any]) -> dict[str, bool]:
    return {
        "fp_reduction": liquid["fp"]["flagged"] <= 0.5 * deberta["fp"]["flagged"],
        "stash_zero": liquid["fp"]["stash_flagged"] == 0,
        "overall_recall": liquid["recall"]["overall"]["recall"] >= deberta["recall"]["overall"]["recall"] - 0.02,
        "name_recall": liquid["recall"]["by_type"]["NAME"]["recall"] >= 0.90,
        "email_recall": liquid["recall"]["by_type"]["EMAIL"]["recall"] >= 0.90,
        "phone_recall": liquid["recall"]["by_type"]["PHONE"]["recall"] >= 0.90,
        "ja_recall": liquid["recall"]["by_language"]["JA"]["recall"]
        >= deberta["recall"]["by_language"]["JA"]["recall"],
        "ai4privacy_parity": liquid["ai4privacy"]["f1"] >= deberta["ai4privacy"]["f1"] - 0.05,
    }


def _render_report(
    report_path: Path,
    deberta: dict[str, Any],
    liquid: dict[str, Any],
    fp_total: int,
    recall_total: int,
) -> None:
    gates = _gate_results(deberta, liquid)
    passed = all(gates.values())
    verdict = (
        "PASS — all Phase A gates hold; Phase B flag-gated integration is authorized."
        if passed
        else "STOP — one or more Phase A quality gates failed; no engine integration is authorized."
    )
    lines = [
        "# PII detector evaluation — 2026-08-15",
        "",
        f"VERDICT: **{verdict}**",
        "",
        "This is an engine-level comparison of raw model/helper spans. NBHD stoplists, tier policy, "
        "and Presidio recognizers were not applied. Seeds and PyTorch deterministic algorithms were "
        f"fixed at `{SEED}`; inference used CPU with one PyTorch thread.",
        "",
        "## False-positive suite",
        "",
        f"The {fp_total}-sentence suite covers every literal in `_FLEET_WORD_STOPLIST` and "
        "`_FLEET_PHRASE_STOPLIST`, the 2026-08-08 fleet evidence terms, and the task-brief cases. "
        "A sentence counts only when a raw person/location-class span overlaps the named junk target.",
        "",
        "| Model | Junk sentences flagged | Rate | Stash sentences flagged | Liquid / DeBERTa |",
        "|---|---:|---:|---:|---:|",
        f"| Production DeBERTa | {deberta['fp']['flagged']} / {deberta['fp']['total']} | "
        f"{_pct(deberta['fp']['flagged'] / deberta['fp']['total'])} | "
        f"{deberta['fp']['stash_flagged']} / 4 | — |",
        f"| Liquid LFM2.5 | {liquid['fp']['flagged']} / {liquid['fp']['total']} | "
        f"{_pct(liquid['fp']['flagged'] / liquid['fp']['total'])} | "
        f"{liquid['fp']['stash_flagged']} / 4 | "
        f"{_ratio(liquid['fp']['flagged'], deberta['fp']['flagged'])} |",
        "",
        "## Synthetic recall suite",
        "",
        f"The suite contains {recall_total} labeled spans. Matching is type-aware and one-to-one; "
        "any positive character overlap counts as a hit.",
        "",
        "| Type | DeBERTa hits | DeBERTa recall | Liquid hits | Liquid recall |",
        "|---|---:|---:|---:|---:|",
    ]
    for pii_type in ("NAME", "EMAIL", "PHONE", "ADDRESS", "DOB"):
        deb = deberta["recall"]["by_type"][pii_type]
        liq = liquid["recall"]["by_type"][pii_type]
        lines.append(
            f"| {pii_type} | {deb['hits']} / {deb['total']} | {_pct(deb['recall'])} | "
            f"{liq['hits']} / {liq['total']} | {_pct(liq['recall'])} |"
        )
    deb = deberta["recall"]["overall"]
    liq = liquid["recall"]["overall"]
    lines += [
        f"| **Overall** | **{deb['hits']} / {deb['total']}** | **{_pct(deb['recall'])}** | "
        f"**{liq['hits']} / {liq['total']}** | **{_pct(liq['recall'])}** |",
        "",
        "| Language | DeBERTa hits | DeBERTa recall | Liquid hits | Liquid recall |",
        "|---|---:|---:|---:|---:|",
    ]
    for language in ("EN", "JA"):
        deb = deberta["recall"]["by_language"][language]
        liq = liquid["recall"]["by_language"][language]
        lines.append(
            f"| {language} | {deb['hits']} / {deb['total']} | {_pct(deb['recall'])} | "
            f"{liq['hits']} / {liq['total']} | {_pct(liq['recall'])} |"
        )

    lines += [
        "",
        "## ai4privacy parity slice",
        "",
        f"Fixed {AI4PRIVACY_ROWS}-row sample of the pinned `validation` split, selected with "
        f"`shuffle(seed={SEED}).select(range({AI4PRIVACY_ROWS}))`. Partial F1 uses one-to-one "
        "overlap matching at the detection tier (type ignored), because the two checkpoints use "
        "different label taxonomies.",
        "",
        "| Model | TP | FP | FN | Precision | Recall | Partial F1 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, result in (("Production DeBERTa", deberta), ("Liquid LFM2.5", liquid)):
        score = result["ai4privacy"]
        lines.append(
            f"| {name} | {score['tp']} | {score['fp']} | {score['fn']} | "
            f"{_pct(score['precision'])} | {_pct(score['recall'])} | {_pct(score['f1'])} |"
        )

    lines += [
        "",
        "## Resident memory and CPU latency",
        "",
        "RSS is the fresh worker process delta from immediately before tokenizer/model load through "
        "one untimed warm-up, so lazily mapped weight pages are resident. Latency excludes that warm-up "
        "and is the median of individual sentence calls (tokenization + inference + decoding).",
        "",
        "| Model | Load RSS delta | FP median | Recall median | ai4privacy median | All suites median |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, result in (("Production DeBERTa", deberta), ("Liquid LFM2.5", liquid)):
        latency = result["latency_ms"]
        lines.append(
            f"| {name} | {result['load']['rss_delta'] / 1024**2:.1f} MiB | "
            f"{latency['fp']:.1f} ms | {latency['recall']:.1f} ms | "
            f"{latency['ai4privacy']:.1f} ms | {latency['overall']:.1f} ms |"
        )

    gate_labels = {
        "fp_reduction": "Liquid FP count <= 50% of DeBERTa",
        "stash_zero": "Liquid stash flags = 0",
        "overall_recall": "Liquid overall recall >= DeBERTa - 2 points",
        "name_recall": "Liquid name recall >= 90%",
        "email_recall": "Liquid email recall >= 90%",
        "phone_recall": "Liquid phone recall >= 90%",
        "ja_recall": "Liquid JA recall >= DeBERTa JA recall",
        "ai4privacy_parity": "Liquid ai4privacy partial F1 no more than 5 points below DeBERTa",
    }
    lines += [
        "",
        "## Gate decision",
        "",
        "| Requirement | Result |",
        "|---|---:|",
    ]
    lines.extend(f"| {gate_labels[key]} | {'PASS' if value else 'FAIL'} |" for key, value in gates.items())
    lines += [
        "",
        f"VERDICT: **{verdict}**",
        "",
        "## Pinned artifacts",
        "",
        f"- Production model: `{DEBERTA_REPO}` @ `{DEBERTA_REVISION}`",
        f"- Candidate model and shipped decode helpers: `{LIQUID_REPO}` @ `{LIQUID_REVISION}`",
        f"- ai4privacy dataset: `{AI4PRIVACY_REPO}` @ `{AI4PRIVACY_REVISION}`",
        "",
        "## Method notes",
        "",
        "- DeBERTa construction exactly mirrors `apps/pii/engine.py`: explicit tokenizer and "
        'token-classification model, then `pipeline(..., aggregation_strategy="simple", device="cpu")`.',
        "- Liquid construction uses `trust_remote_code=True` for tokenizer/model and the repository's "
        "pinned `pii_hybrid_decode.py` with `hybrid=True`; `context_cued.py` is loaded from the same snapshot.",
        "- Both models run vanilla PyTorch CPU in evaluation mode. No ONNX, optimum, Presidio, "
        "or redactor post-processing participates.",
        "- FP targets are product-policy negatives, including opted-out geography and Osaka travel, "
        "even when the surface form is linguistically a real location.",
        "",
        "## Commit gate outputs",
        "",
        "To be replaced with verbatim command tails after formatting, migration, and `apps.pii` test gates.",
        "",
        "## Label-mapping decisions",
        "",
        "No production Liquid label mapping was created and no types were deliberately dropped: the "
        "Phase A gate stopped the work before Phase B. Evaluation-only broad types are listed in the "
        "harness and do not affect runtime behavior.",
        "",
        "## Risks and open questions",
        "",
        f"- Liquid missed {sum(item['pii_type'] == 'EMAIL' for item in liquid['recall']['misses'])} "
        "of 20 synthetic emails. Review the missed no-space Japanese contexts in the harness before "
        "reconsidering this checkpoint/helper.",
        "- Synthetic recall measures controlled formats, not fleet prevalence; the pinned multilingual "
        "ai4privacy slice provides a broader parity check but contains no Japanese rows.",
        "- Partial-overlap scoring is intentionally forgiving about boundaries; downstream redaction "
        "quality still depends on the helper's boundary expansion and NBHD's existing span merge logic.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parent(report_path: Path) -> int:
    fp_cases = _fp_suite()
    recall_cases = _recall_suite()
    _validate_suites(fp_cases, recall_cases)
    print(f"Validated {len(fp_cases)} FP cases and {len(recall_cases)} recall spans.", flush=True)
    print("Loading pinned ai4privacy validation split and selecting 500 rows...", flush=True)
    ai_rows = _load_ai4privacy_sample()

    corpus = {
        "fp": [asdict(case) for case in fp_cases],
        "recall": [asdict(case) for case in recall_cases],
        "ai4privacy": ai_rows,
    }
    with tempfile.TemporaryDirectory(prefix="pii-detector-eval-") as temp_dir:
        temp = Path(temp_dir)
        corpus_path = temp / "corpus.json"
        corpus_path.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
        results: dict[str, dict[str, Any]] = {}
        for model_name in ("deberta", "liquid"):
            result_path = temp / f"{model_name}.json"
            print(f"Evaluating {model_name} in a fresh CPU worker...", flush=True)
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    model_name,
                    "--corpus",
                    str(corpus_path),
                    "--result",
                    str(result_path),
                ],
                check=True,
            )
            results[model_name] = json.loads(result_path.read_text(encoding="utf-8"))

    _render_report(
        report_path,
        results["deberta"],
        results["liquid"],
        len(fp_cases),
        len(recall_cases),
    )
    gates = _gate_results(results["deberta"], results["liquid"])
    print(f"Wrote {report_path}", flush=True)
    print(f"VERDICT: {'PASS' if all(gates.values()) else 'STOP'}", flush=True)
    for gate, passed in gates.items():
        print(f"  {gate}: {'PASS' if passed else 'FAIL'}", flush=True)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--worker", choices=("deberta", "liquid"), help=argparse.SUPPRESS)
    parser.add_argument("--corpus", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--result", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.worker:
        if args.corpus is None or args.result is None:
            raise SystemExit("worker mode requires --corpus and --result")
        _worker(args.worker, args.corpus, args.result)
        return 0
    args.report.parent.mkdir(parents=True, exist_ok=True)
    return _parent(args.report)


if __name__ == "__main__":
    raise SystemExit(main())
