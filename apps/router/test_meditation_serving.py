"""Tests for the Range-aware meditation audio response.

``serve_meditation_audio`` itself talks to the Azure File Share; the HTTP
semantics that matter for the iOS/Safari "meditation never stops" bug live in the
pure ``_audio_range_response`` helper, which is what these tests pin: advertise
``Accept-Ranges`` and answer ``Range:`` with a 206 so AVPlayer can discover a
finite duration and stop at the true end of the file.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.router.views import _audio_range_response

_DATA = bytes(range(256)) * 10  # 2560 deterministic bytes
_SIZE = len(_DATA)


class AudioRangeResponseTests(SimpleTestCase):
    def test_no_range_is_full_200_with_accept_ranges(self):
        resp = _audio_range_response(_DATA, "audio/mpeg", "")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Accept-Ranges"], "bytes")
        self.assertEqual(resp.content, _DATA)

    def test_closed_range_returns_206(self):
        resp = _audio_range_response(_DATA, "audio/mpeg", "bytes=0-99")
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp["Accept-Ranges"], "bytes")
        self.assertEqual(resp["Content-Range"], f"bytes 0-99/{_SIZE}")
        self.assertEqual(resp.content, _DATA[:100])

    def test_open_ended_range_runs_to_eof(self):
        resp = _audio_range_response(_DATA, "audio/mpeg", "bytes=100-")
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp["Content-Range"], f"bytes 100-{_SIZE - 1}/{_SIZE}")
        self.assertEqual(resp.content, _DATA[100:])

    def test_suffix_range_returns_last_n_bytes(self):
        resp = _audio_range_response(_DATA, "audio/mpeg", "bytes=-50")
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.content, _DATA[-50:])
        self.assertEqual(resp["Content-Range"], f"bytes {_SIZE - 50}-{_SIZE - 1}/{_SIZE}")

    def test_unsatisfiable_range_is_416(self):
        resp = _audio_range_response(_DATA, "audio/mpeg", "bytes=999999-")
        self.assertEqual(resp.status_code, 416)
        self.assertEqual(resp["Content-Range"], f"bytes */{_SIZE}")

    def test_malformed_range_falls_back_to_full_200(self):
        resp = _audio_range_response(_DATA, "audio/mpeg", "bytes=abc-def")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, _DATA)
