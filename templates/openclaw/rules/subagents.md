# Sub-Agent Rules

- Delegate only work likely to take more than about 30 seconds: multi-step
  research, long-document analysis, or a large generation. Answer simple
  questions directly.
- After spawning, acknowledge immediately in one short line: “On it — I'll let
  you know when it's ready.” Do not yield the app turn.
- Never pass `context: "fork"` to `sessions_spawn`; give the helper only the
  bounded context it needs.
- A helper reports to you; it must not send, create, publish, or otherwise act
  outward on the user's behalf.
- When an `[Internal task completion event]` arrives in an app thread, send the
  final update through `nbhd_send_to_user` with the requester `thread_id`. Write
  in your normal voice and omit raw run, session, and delivery metadata.
- Never answer `NO_REPLY`, `no_reply`, or `ANNOUNCE_SKIP` to an app completion.
  The user has not seen the result yet.
- If the helper timed out or failed, send a short honest update with the
  one-line reason when available and offer to retry.
