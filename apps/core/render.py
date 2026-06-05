"""Core meditation render engine — manifest → guided-meditation audio.

Deterministic executor for the Core pillar. The assistant authors a render
*manifest* (judgment); this module voices it via Gemini TTS and stitches in the
silences with ffmpeg (deterministic). That split is the project invariant
"backend computes evidence, the LLM makes judgments".

Ported from the de-risking prototype (``core_prototype/render.py``, which MJ
heard real output from), but restructured so the **pure** planning / validation
/ timing math is unit-testable without ffmpeg or a TTS key, and the I/O
primitives (the TTS call, every ffmpeg subprocess) are module-level functions
that tests can patch individually.

Invariants baked in (verified Gemini TTS constraints — see CONTINUITY_core.md):

* Silence is produced by ffmpeg (``anullsrc``), NEVER requested from the model
  (Gemini has no reliable long-pause control / no SSML ``<break>``).
* One pinned voice + model + style prefix on every segment (cross-segment
  consistency).
* No custom temperature (a known trigger for the late-generation silence bug).
* Per-call timeout + retry — a stalled TTS call raises and retries, never hangs
  the whole render (which runs inside a QStash-triggered request handler).
* Per-segment NON-FATAL fallback — one bad line becomes a short silence
  placeholder so a flaky preview model can't sink a 20-segment render.
* Abort the whole render on quota (HTTP 429) — retrying won't help.
* Each speech segment is short, keeping every call inside the multi-minute
  quality window.

The module imports only the stdlib at top level; ``google-genai`` is imported
lazily inside the TTS path so mock renders, the pure functions, and CI (which
has no key) never touch it.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
import subprocess
import tempfile
import time
import wave
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---- audio constants (Gemini TTS emits PCM 24kHz / 16-bit / mono) ----------
SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_FMT = "s16"
WORDS_PER_SEC = 2.5  # ~150 wpm — an unhurried meditation cadence
JOIN_FADE = 0.012  # 12ms fades at joins to kill concat clicks
FLEX_FLOOR = 1.0  # a flex silence never collapses below this
FLEX_CEIL = 150.0  # ...nor balloons past this. Generous: a meditation often wants a
# minutes-long hold, and the composer decides where those belong — silence is free.
# Per-call ceiling kept small so a single stalled segment (45s x 3 attempts +
# ~6s backoff ≈ 141s) stays well under the ~300s synchronous-handler budget.
TTS_TIMEOUT_MS = 45_000
TTS_ATTEMPTS = 3
# Rate-limit (429) handling: on a low Gemini tier the per-minute cap throttles
# bursts. Treat 429 as TRANSIENT — back off (respecting the server's retryDelay
# when present) and retry, so the render paces itself under the cap instead of
# aborting. Only after exhausting these does the segment fall back to a silence
# placeholder (non-fatal); a render where most segments were throttled is failed
# by the caller rather than shipped near-silent.
TTS_RATE_LIMIT_RETRIES = 6
TTS_RATE_LIMIT_BACKOFF_CAP_S = 40.0
# Render-wide soft deadline: once exceeded, no NEW TTS call starts (the segment
# degrades to a silence placeholder), so a TTS outage / sustained throttle can't
# push the whole render past the handler budget. In-flight calls still finish
# within TTS_TIMEOUT_MS; concat/transcode adds ~10-20s — 240s keeps the worst
# case (~240 + 45 + ~20) under the ~300s gunicorn budget.
DEFAULT_RENDER_DEADLINE_S = 240.0
DEFAULT_MODEL = "gemini-2.5-flash-preview-tts"  # cheapest + accessible on low tiers (~$0.19/render)
DEFAULT_VOICE = "Achernar"  # calm; Aoede ("breezy") is the other candidate

# ---- manifest schema bounds ------------------------------------------------
REQUIRED_PHASES = [
    "arrival",
    "breath_anchor",
    "body_scan",
    "core_practice",
    "integration",
    "closing",
]
SILENCE_MIN = 3.0
SILENCE_MAX = 150.0  # was 30 — a guided sit often holds in silence for a minute or more;
# the composer chooses these durations holistically (silence is free to render).
MAX_SPEECH_CHARS = 600  # one narration line longer than this is almost certainly a mistake
# Bound the *count* of spoken segments (not their placement). Floor keeps a sit from
# being near-empty; ceiling caps TTS calls so a render stays under the low-tier Gemini
# per-minute cap + the render deadline. Where the words fall is the composer's call.
MIN_SPEECH_SEGMENTS = 4
MAX_SPEECH_SEGMENTS = 12


class ManifestError(ValueError):
    """Manifest failed validation — terminal (re-rendering won't fix it)."""


class QuotaExceeded(RuntimeError):
    """HTTP 429 from Gemini TTS — abort the whole render (retry won't help)."""


# ============================================================================
# Pure: validation, planning, timing math (no ffmpeg, no network — unit tests
# cover these directly).
# ============================================================================


def estimate_speech_seconds(text: str) -> float:
    """Rough narration length used to validate TTS output isn't truncated."""
    return max(1.2, len(text.split()) / WORDS_PER_SEC)


def validate_manifest(manifest: object) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid).

    The composer owns the *rhythm* — where words land, where silence falls, and how
    long each silence holds — so validation only guards what the renderer needs and
    what protects the spend, NOT the artistic shape:

    * all 6 phases present, in order;
    * every explicit ``silence.seconds`` in ``[3, 150]`` (or the literal ``"flex"``);
      long holds are fine — a phase may be mostly, or entirely, silence;
    * ``closing`` ENDS on a (non-empty) speech segment — so it lands rather than
      trailing off into silence;
    * any ``speech`` segment present is non-empty and within ``MAX_SPEECH_CHARS``;
    * the *count* of spoken segments across the whole sit is in
      ``[MIN_SPEECH_SEGMENTS, MAX_SPEECH_SEGMENTS]`` — a floor so it isn't near-empty,
      a ceiling so TTS calls stay under the rate cap;
    * each phase declares a positive ``target_seconds`` (the budget a ``flex`` silence
      fills, when one is used).

    Deliberately NOT enforced anymore: one-speech-per-phase, and a flex-per-phase. A
    phase can be pure silence; a phase can pin its own length with explicit silences.
    """
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]
    phases = manifest.get("phases")
    if not isinstance(phases, list) or not phases:
        return ["manifest.phases must be a non-empty list"]

    names = [p.get("name") if isinstance(p, dict) else None for p in phases]
    if names != REQUIRED_PHASES:
        # Positional checks below assume the canonical arc — bail early.
        return [f"phases must be exactly, in order: {REQUIRED_PHASES}; got {names}"]

    errors: list[str] = []
    speech_count = 0
    for phase in phases:
        name = phase.get("name")
        segments = phase.get("segments")
        if not isinstance(segments, list) or not segments:
            errors.append(f"phase '{name}' has no segments")
            continue

        for seg in segments:
            if not isinstance(seg, dict):
                errors.append(f"phase '{name}' has a non-object segment")
                continue
            seg_type = seg.get("type")
            if seg_type == "speech":
                text = (seg.get("text") or "").strip()
                if not text:
                    errors.append(f"phase '{name}' has an empty speech segment")
                elif len(text) > MAX_SPEECH_CHARS:
                    errors.append(f"phase '{name}' speech segment too long ({len(text)} > {MAX_SPEECH_CHARS} chars)")
                else:
                    speech_count += 1
            elif seg_type == "silence":
                seconds = seg.get("seconds")
                if seconds == "flex":
                    continue
                try:
                    value = float(seconds)
                except (TypeError, ValueError):
                    errors.append(f"phase '{name}' silence has non-numeric seconds: {seconds!r}")
                    continue
                if not (SILENCE_MIN <= value <= SILENCE_MAX):
                    errors.append(f"phase '{name}' explicit silence {value}s out of [{SILENCE_MIN}, {SILENCE_MAX}]")
            else:
                errors.append(f"phase '{name}' has an unknown segment type: {seg_type!r}")

        try:
            target = float(phase.get("target_seconds"))
            if target <= 0:
                errors.append(f"phase '{name}' target_seconds must be positive")
        except (TypeError, ValueError):
            errors.append(f"phase '{name}' has a missing/invalid target_seconds")

    if speech_count < MIN_SPEECH_SEGMENTS:
        errors.append(f"too few spoken segments ({speech_count} < {MIN_SPEECH_SEGMENTS})")
    elif speech_count > MAX_SPEECH_SEGMENTS:
        errors.append(
            f"too many spoken segments ({speech_count} > {MAX_SPEECH_SEGMENTS}) — would risk the TTS rate cap"
        )

    closing_segments = phases[-1].get("segments") or []
    last = closing_segments[-1] if closing_segments else None
    if not (isinstance(last, dict) and last.get("type") == "speech" and (last.get("text") or "").strip()):
        errors.append("closing phase must END on a (non-empty) speech segment")

    return errors


@dataclass
class PlannedSegment:
    """One ordered piece of the render. ``kind`` is speech | silence | flex.

    ``seconds`` carries the duration for a fixed silence; flex silences are
    resolved per-phase after the speech is measured (see ``reconcile_phase_flex``).
    """

    phase: str
    kind: str
    text: str = ""
    tone: str = ""
    seconds: float = 0.0


def plan_segments(manifest: dict) -> list[PlannedSegment]:
    """Flatten the manifest into an ordered list of segments (the stitch order)."""
    plan: list[PlannedSegment] = []
    for phase in manifest["phases"]:
        name = phase["name"]
        for seg in phase["segments"]:
            if seg.get("type") == "speech":
                plan.append(
                    PlannedSegment(
                        phase=name,
                        kind="speech",
                        text=(seg.get("text") or "").strip(),
                        tone=(seg.get("tone") or "").strip(),
                    )
                )
            elif seg.get("seconds") == "flex":
                plan.append(PlannedSegment(phase=name, kind="flex"))
            else:
                plan.append(PlannedSegment(phase=name, kind="silence", seconds=float(seg["seconds"])))
    return plan


def reconcile_phase_flex(target_seconds: float, speech_total: float, fixed_silence: float, flex_count: int) -> float:
    """Seconds for EACH flex silence so the phase lands on its time budget.

    ``flex = (target - speech - fixed_silence) / flex_count``, clamped to
    ``[FLEX_FLOOR, FLEX_CEIL]``. Splitting across a phase's flex points avoids one
    long dead-air block. Returns 0.0 when the phase has no flex (it then renders
    at its natural length).
    """
    if flex_count <= 0:
        return 0.0
    remaining = target_seconds - speech_total - fixed_silence
    return min(FLEX_CEIL, max(FLEX_FLOOR, remaining / flex_count))


def flatten_guidance_text(manifest: dict) -> str:
    """All speech text, in order, for the stored ``guidance_text`` (display/audit)."""
    lines: list[str] = []
    for phase in manifest["phases"]:
        for seg in phase["segments"]:
            if seg.get("type") == "speech":
                text = (seg.get("text") or "").strip()
                if text:
                    lines.append(text)
    return "\n\n".join(lines)


# ============================================================================
# I/O primitives — every ffmpeg subprocess + the Gemini TTS call. Module-level
# so the orchestration test can patch them; the real-ffmpeg test exercises them.
# ============================================================================


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({' '.join(cmd[:3])} ...): {proc.stderr[-800:]}")


def _ffprobe_seconds(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr[-400:]}")
    return float(proc.stdout.strip())


def _make_silence(seconds: float, dst: Path) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={SAMPLE_RATE}:cl=mono",
            "-t",
            f"{seconds:.3f}",
            "-sample_fmt",
            SAMPLE_FMT,
            str(dst),
        ]
    )


def _bake_fades(src: Path, dst: Path) -> None:
    """Tiny in/out fades so speech<->silence joins don't click."""
    dur = _ffprobe_seconds(src)
    out_start = max(0.0, dur - JOIN_FADE)
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-af",
            f"afade=t=in:st=0:d={JOIN_FADE},afade=t=out:st={out_start:.3f}:d={JOIN_FADE}",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "-sample_fmt",
            SAMPLE_FMT,
            str(dst),
        ]
    )


def _make_mock_speech(text: str, dst: Path) -> None:
    """Placeholder narration: a quiet low tone sized to the estimated length.

    Used by mock renders (no Gemini key/deps) so the stitch / silence / timing /
    transcode chain can be exercised end-to-end — including in the real-ffmpeg
    test.
    """
    seconds = estimate_speech_seconds(text)
    raw = dst.with_suffix(".raw.wav")
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=150:duration={seconds:.3f}",
            "-af",
            "volume=-22dB,vibrato=f=5:d=0.4",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "-sample_fmt",
            SAMPLE_FMT,
            str(raw),
        ]
    )
    _bake_fades(raw, dst)
    raw.unlink(missing_ok=True)


def make_gemini_client(api_key: str, *, timeout_ms: int = TTS_TIMEOUT_MS):
    """Construct a google-genai client with a per-request timeout (lazy import)."""
    from google import genai
    from google.genai import types

    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))


def _extract_audio(resp) -> bytes | None:
    """Pull PCM bytes from a TTS response; None if empty / blocked / malformed.

    Gemini occasionally returns a candidate with no content/parts (or a text token
    instead of audio); indexing it blindly raises ``NoneType has no attribute
    parts``. Returning None lets the caller treat it as a retryable miss.
    """
    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        return None
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    if not parts:
        return None
    return getattr(getattr(parts[0], "inline_data", None), "data", None)


_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def _is_rate_limit(err: str) -> bool:
    return "RESOURCE_EXHAUSTED" in err or "429" in err


def _rate_limit_delay(err: str, attempt: int) -> float:
    """Seconds to wait before retrying a 429. Prefer the server's ``retryDelay``
    (e.g. ``'retryDelay': '34s'``); otherwise exponential backoff (5,10,20,40…)
    capped at ``TTS_RATE_LIMIT_BACKOFF_CAP_S``. ``attempt`` is 1-based."""
    match = _RETRY_DELAY_RE.search(err)
    if match:
        try:
            return min(TTS_RATE_LIMIT_BACKOFF_CAP_S, max(1.0, float(match.group(1)) + 1.0))
        except ValueError:
            pass
    return min(TTS_RATE_LIMIT_BACKOFF_CAP_S, 5.0 * (2 ** (attempt - 1)))


def render_gemini_segment(
    client, text: str, voice: str, model: str, style: str, dst: Path, *, attempts: int = TTS_ATTEMPTS
) -> None:
    """Render one narration segment to ``dst`` (24k/mono wav with join fades).

    Retries on blank / short output; backs off on transient errors. On 429 it
    backs off (respecting the server's ``retryDelay``) and retries up to
    ``TTS_RATE_LIMIT_RETRIES`` times so a low-tier per-minute cap is paced rather
    than fatal; only after exhausting those does it raise ``QuotaExceeded`` — a
    PER-SEGMENT signal the caller turns into a non-fatal placeholder (the whole
    render is failed only if most segments were throttled).
    """
    from google.genai import types

    prompt = (
        "Read the following aloud in a soft, calm, slow, soothing "
        f"meditation-guide voice. {style}. Do not read these instructions aloud.\n\n"
        f"{text}"
    )
    expected = estimate_speech_seconds(text)
    last_err = "unknown"
    transient_left = attempts
    rate_limit_left = TTS_RATE_LIMIT_RETRIES
    while transient_left > 0:
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    # NOTE: no temperature set on purpose (silence-bug trigger).
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                        )
                    ),
                ),
            )
            pcm = _extract_audio(resp)
            if not pcm:
                raise RuntimeError("empty/blocked response (no audio)")
            raw = dst.with_suffix(".raw.wav")
            with wave.open(str(raw), "wb") as wav_out:
                wav_out.setnchannels(CHANNELS)
                wav_out.setsampwidth(2)  # 16-bit
                wav_out.setframerate(SAMPLE_RATE)
                wav_out.writeframes(pcm)
            got = _ffprobe_seconds(raw)
            if got < max(0.8, 0.5 * expected):
                raw.unlink(missing_ok=True)
                raise RuntimeError(f"output too short ({got:.1f}s < ~{expected:.1f}s)")
            _bake_fades(raw, dst)
            raw.unlink(missing_ok=True)
            return
        except Exception as exc:  # noqa: BLE001 — transient TTS errors: backoff + retry
            last_err = str(exc)[:200]
            if _is_rate_limit(last_err):
                # 429 rate limit — back off (NOT counted against the transient
                # budget) so a low-tier per-minute cap is paced, not aborted.
                rate_limit_left -= 1
                if rate_limit_left <= 0:
                    raise QuotaExceeded(last_err) from exc
                delay = _rate_limit_delay(last_err, TTS_RATE_LIMIT_RETRIES - rate_limit_left)
                logger.info(
                    "Core render: 429 rate-limited — backing off %.0fs (%d rate-limit retries left)",
                    delay,
                    rate_limit_left,
                )
                time.sleep(delay)
                continue
            transient_left -= 1
            if transient_left <= 0:
                break
            time.sleep(min(2 ** (attempts - transient_left), 10))
    raise RuntimeError(f"failed after retries: {last_err}")


def _concat_and_master(wavs: list[Path], workdir: Path, out_mp3: Path, out_ogg: Path | None) -> None:
    """Concat the ordered wavs, loudness-normalize, transcode to mp3 (+ ogg/opus)."""
    listing = workdir / "concat.txt"
    listing.write_text("".join(f"file '{w.resolve()}'\n" for w in wavs))
    joined = workdir / "joined.wav"
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(joined)])

    loudnorm = "loudnorm=I=-16:TP=-1.5:LRA=11"
    _run(["ffmpeg", "-y", "-i", str(joined), "-af", loudnorm, "-codec:a", "libmp3lame", "-q:a", "4", str(out_mp3)])
    if out_ogg is not None:
        _run(["ffmpeg", "-y", "-i", str(joined), "-af", loudnorm, "-c:a", "libopus", "-b:a", "48k", str(out_ogg)])


# ============================================================================
# Orchestrator
# ============================================================================


@dataclass
class RenderResult:
    mp3_bytes: bytes
    ogg_bytes: bytes | None
    duration_ms: int
    guidance_text: str
    speech_count: int
    failed_count: int  # segments that fell back to a silence placeholder (any cause)
    quota_failed_count: int = 0  # subset of failed_count that exhausted 429 backoff retries


def render_manifest_to_audio(
    manifest: dict,
    *,
    voice: str,
    model: str,
    api_key: str | None = None,
    concurrency: int = 4,
    deadline_seconds: float = DEFAULT_RENDER_DEADLINE_S,
    mock: bool = False,
    want_ogg: bool = True,
) -> RenderResult:
    """Render a validated manifest to audio bytes.

    Speech segments are independent (silence is inserted by ffmpeg between them),
    so they render concurrently — bounded by ``concurrency`` for preview rate
    limits. A segment that fails every retry — that would START a new TTS call
    after ``deadline_seconds`` has elapsed, or that exhausts its 429 backoff
    retries — falls back to a 1.2s silence placeholder (non-fatal), so neither a
    flaky line, a TTS outage, nor a low-tier rate cap can sink or overrun the
    render. ``RenderResult.quota_failed_count`` reports how many were rate-limited
    out so the caller can fail a mostly-throttled render instead of shipping it
    near-silent.

    Returns mp3 (+ optional ogg) bytes, the measured duration, the flattened
    guidance text, and failure counts. Raises ``ManifestError`` for an invalid
    manifest or a missing key.
    """
    errors = validate_manifest(manifest)
    if errors:
        raise ManifestError("; ".join(errors))
    if not mock and not api_key:
        raise ManifestError("no TTS api_key provided")

    style = (manifest.get("global_tone") or "").strip()
    plan = plan_segments(manifest)
    client = None if mock else make_gemini_client(api_key)

    speech_indexes = [i for i, seg in enumerate(plan) if seg.kind == "speech"]

    with tempfile.TemporaryDirectory(prefix="core_render_") as tmp:
        workdir = Path(tmp)
        speech_paths: dict[int, Path] = {}
        speech_dur: dict[int, float] = {}
        failed = 0
        quota_failed = 0
        start = time.monotonic()

        def _measure(wav: Path, *, fallback: float) -> float:
            # A probe hiccup on an already-written file must not abort the render
            # (the file is still concat-able; only the flex-timing estimate is off).
            try:
                return _ffprobe_seconds(wav)
            except Exception:  # noqa: BLE001
                logger.warning("Core render: probe failed for %s; using %.1fs estimate", wav.name, fallback)
                return fallback

        def render_one(index: int) -> tuple[int, Path, float, bool, bool]:
            seg = plan[index]
            wav = workdir / f"seg_{index:03d}_{seg.phase}_speech.wav"
            seg_style = "; ".join(part for part in (style, seg.tone) if part)
            quota = False
            try:
                # Render-wide deadline: don't START a new TTS call past the budget
                # (an in-flight call is still bounded by TTS_TIMEOUT_MS).
                if not mock and (time.monotonic() - start) > deadline_seconds:
                    raise TimeoutError(f"render deadline {deadline_seconds:.0f}s exceeded")
                if mock:
                    _make_mock_speech(seg.text, wav)
                else:
                    render_gemini_segment(client, seg.text, voice, model, seg_style, wav)
                # Measure inside the try so a probe failure degrades, never aborts.
                return index, wav, _measure(wav, fallback=estimate_speech_seconds(seg.text)), True, False
            except QuotaExceeded:
                # Segment exhausted its 429 backoff retries — non-fatal placeholder.
                # The whole render is failed by the caller only if MOST segments
                # were throttled (else a few rate-limited lines just go silent).
                logger.warning(
                    "Core render: segment %d (%s) rate-limited out -> 1.2s silence placeholder", index, seg.phase
                )
                quota = True
            except Exception as exc:  # noqa: BLE001 — non-fatal: placeholder + carry on
                logger.warning(
                    "Core render: segment %d (%s) failed (%s) -> 1.2s silence placeholder",
                    index,
                    seg.phase,
                    str(exc)[:80],
                )
            # Placeholder MUST exist for the concat step. If _make_silence itself
            # raises, ffmpeg is fundamentally broken — let it propagate so the
            # session is marked FAILED and QStash retries (a transient infra fault).
            _make_silence(1.2, wav)
            return index, wav, _measure(wav, fallback=1.2), False, quota

        if speech_indexes:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
                futures = {pool.submit(render_one, i): i for i in speech_indexes}
                for future in concurrent.futures.as_completed(futures):
                    index, wav, dur, ok, quota = future.result()
                    speech_paths[index] = wav
                    speech_dur[index] = dur
                    if not ok:
                        failed += 1
                    if quota:
                        quota_failed += 1

        # ---- flex reconciliation per phase (split slack across flex points) ----
        phase_speech: dict[str, float] = defaultdict(float)
        phase_fixed: dict[str, float] = defaultdict(float)
        phase_flex_count: dict[str, int] = defaultdict(int)
        phase_target = {p["name"]: float(p["target_seconds"]) for p in manifest["phases"]}
        for index, seg in enumerate(plan):
            if seg.kind == "speech":
                phase_speech[seg.phase] += speech_dur.get(index, 0.0)
            elif seg.kind == "silence":
                phase_fixed[seg.phase] += seg.seconds
            elif seg.kind == "flex":
                phase_flex_count[seg.phase] += 1
        phase_flex_secs = {
            name: reconcile_phase_flex(
                phase_target[name], phase_speech[name], phase_fixed[name], phase_flex_count[name]
            )
            for name in phase_target
        }

        # ---- assemble in manifest order ----
        ordered: list[Path] = []
        for index, seg in enumerate(plan):
            if seg.kind == "speech":
                ordered.append(speech_paths[index])
            else:
                seconds = phase_flex_secs[seg.phase] if seg.kind == "flex" else seg.seconds
                silence = workdir / f"sil_{index:03d}_{seg.phase}.wav"
                _make_silence(seconds, silence)
                ordered.append(silence)

        out_mp3 = workdir / "meditation.mp3"
        out_ogg = workdir / "meditation.ogg" if want_ogg else None
        _concat_and_master(ordered, workdir, out_mp3, out_ogg)
        total = _ffprobe_seconds(out_mp3)
        mp3_bytes = out_mp3.read_bytes()
        ogg_bytes = out_ogg.read_bytes() if out_ogg is not None else None

    return RenderResult(
        mp3_bytes=mp3_bytes,
        ogg_bytes=ogg_bytes,
        duration_ms=int(round(total * 1000)),
        guidance_text=flatten_guidance_text(manifest),
        speech_count=len(speech_indexes),
        failed_count=failed,
        quota_failed_count=quota_failed,
    )
