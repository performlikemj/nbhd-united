"""Frozen AAD coordinates for ciphertext-only transcript content.

Unlike the legacy dual-write declarations, this greenfield model has no
plaintext sibling field. The stored ciphertext field is therefore also its
logical AAD column, as required by the transcript-memory directive.
"""

TRANSCRIPT_EVENT_TEXT: tuple[str, str] = (
    "transcripts_transcriptevent",
    "text_enc",
)
