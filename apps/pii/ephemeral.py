"""Checked batch redaction for ephemeral egress: no ORM, registry writes or receipts.

Existing bindings are read-only inputs. Newly detected entities and counters
live only in this batch; normal chat still owns permanent/provisional minting.
"""

import re
from dataclasses import replace
from time import monotonic

from apps.pii import redactor


def _detect_batch(text, session, deadline):
    """Pack fields into bounded windows so batching cannot truncate the tail.

    The shared service's DeBERTa callable does not expose tokenizer overflow.
    A conservative UTF-8 byte bound stays below its 512-token window, with
    whitespace boundaries and overlap for multiword entities. Refuse a token
    too long to fit rather than claim confirmation of a truncated detection.
    """
    hits = []
    start = 0
    while start < len(text):
        end = start + len(text[start:].encode("utf-8")[:384].decode("utf-8", errors="ignore"))
        if end < len(text):
            boundaries = list(re.finditer(r"\s+", text[start:end]))
            if not boundaries:
                return None
            end = start + boundaries[-1].end()
        redactor._reset_neural_detector_outcome()
        found = redactor._detect_pii(text[start:end], session.entities, session.score_threshold, deadline=deadline)
        if monotonic() >= deadline:
            raise TimeoutError("Redaction deadline exceeded")
        if redactor._neural_detector_available() is not True:
            return None
        window_hits = [replace(hit, start=hit.start + start, end=hit.end + start) for hit in found]
        # A name cut at a window edge can conflict with its full detection in
        # the overlap. Do not let normal shorter-span tie-breaking expose the
        # suffix: ambiguous cross-window boundaries cannot be confirmed.
        if any(
            prior.start < hit.end and hit.start < prior.end and (prior.start, prior.end) != (hit.start, hit.end)
            for hit in window_hits
            for prior in hits
        ):
            return None
        hits.extend(window_hits)
        if end == len(text):
            break
        overlap_start = max(start + 1, end - 64)
        boundary = re.search(r"\s+", text[overlap_start:end])
        start = overlap_start + boundary.end() if boundary else end
    return hits


def redact_texts_ephemeral_checked(texts: list[str], tenant, *, deadline: float) -> list[redactor.RedactionOutcome]:
    """Batched detection, consistent request-local placeholders, fail closed.

    A caller must bound the wall time of local inference (which cannot be
    interrupted safely). The shared detector receives the absolute deadline.
    No raw input or exception content is logged on this path.
    """

    def unavailable(reason):
        return [redactor.RedactionOutcome("", False, reason) for _ in texts]

    try:
        if monotonic() >= deadline:
            raise TimeoutError("Redaction deadline exceeded")
        session = redactor.RedactionSession(tenant=tenant)
        if not session.enabled or not session.entities or any(not text.strip() for text in texts):
            return unavailable("redaction-unconfirmed")
        # Reuse confirmed known-value substitutions even when the detector
        # misses a familiar name. These helpers never modify the registry.
        prepared = [redactor._replace_known_only(text, session._inverted_ci, session._denylist) for text in texts]
        separator = "\n\n---\n\n"
        joined = separator.join(prepared)
        hits = _detect_batch(joined, session, deadline)
        if hits is None:
            return unavailable("neural-unavailable")
        # Never guess which field owns a detection crossing a batch boundary.
        slices = []
        offset = 0
        for text in prepared:
            slices.append((offset, offset + len(text)))
            offset += len(text) + len(separator)
        if any(not any(start <= hit.start < hit.end <= end for start, end in slices) for hit in hits):
            return unavailable("cross-field-span")
        outcomes = []
        for text, (start, end) in zip(prepared, slices):
            field_hits = [
                replace(hit, start=hit.start - start, end=hit.end - start) for hit in hits if start <= hit.start < end
            ]
            field_hits = redactor._filter_results(field_hits, text, set(), denylist=session._denylist)
            ranges = [(m.start(), m.end()) for m in redactor._PLACEHOLDER_RE.finditer(text)]
            replacements = []
            for hit in sorted(field_hits, key=lambda item: item.start):
                if redactor._hit_inside_placeholder(hit, ranges):
                    continue
                original = text[hit.start : hit.end]
                key = redactor._canonical_key(original)
                known = session._inverted_ci.get(key)
                if known is not None:
                    placeholder = known[1]
                else:
                    count = session._type_counters.get(hit.entity_type, 0) + 1
                    session._type_counters[hit.entity_type] = count
                    placeholder = f"[{hit.entity_type}_{count}]"
                    session._inverted_ci[key] = (original, placeholder)
                replacements.append((hit.start, hit.end, placeholder))
            for begin, finish, placeholder in reversed(replacements):
                text = text[:begin] + placeholder + text[finish:]
            outcomes.append(redactor.RedactionOutcome(text, True, "redacted-ephemeral"))
        if monotonic() >= deadline:
            raise TimeoutError("Redaction deadline exceeded")
        # Detection may find only one mention. Reapply the complete local map
        # across all fields so repeated newly discovered names are masked too.
        return [
            redactor.RedactionOutcome(
                redactor._replace_known_only(outcome.text, session._inverted_ci, session._denylist),
                True,
                "redacted-ephemeral",
            )
            for outcome in outcomes
        ]
    except TimeoutError:
        raise TimeoutError("Redaction deadline exceeded") from None
    except Exception:
        return unavailable("redaction-error")
