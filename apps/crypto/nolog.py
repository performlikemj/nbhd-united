"""RedactedStr — encryption-at-rest Phase 1 (PR3).

Red-team finding 18: f-strings and `logger.info("%s", x)` invoke an object's
`__str__`/`__format__`, NOT `__repr__`. A type that only redacts `__repr__`
(the usual "don't leak this in a traceback" trick) is a false guarantee here
— any accidental f-string or %-log of a decrypted value would still print the
plaintext. `RedactedStr` redacts every render path; `.reveal()` is the one
deliberate way back to the real string.
"""

from __future__ import annotations


class RedactedStr(str):
    """A decrypted value that refuses to print itself.

    Subclasses `str` so it still compares, hashes, and slices like a normal
    string for internal logic that needs it — but `str()`, f-strings,
    `%s`-formatting, `.format()`, and `repr()` all show only a length marker,
    never content. (Any prod code that *compares* a `RedactedStr` is a smell,
    not this module's concern in Phase 1 — Phase 1 has zero decrypt
    consumers.)
    """

    def __repr__(self) -> str:
        return f"‹redacted:{len(self)}c›"

    def __str__(self) -> str:
        return f"‹redacted:{len(self)}c›"

    def __format__(self, format_spec: str) -> str:
        return self.__str__()

    def reveal(self) -> str:
        """The ONLY sanctioned way to get the real plaintext back out."""
        return str.__str__(self)
