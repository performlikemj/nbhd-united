-- ============================================================================
-- DATA REPAIR — MJ tenant "Rakuten" → "Rocketen" voice-transcription garble
-- ============================================================================
--
--   DO NOT RUN AGAINST PRODUCTION UNTIL REVIEWED AND APPROVED.
--
-- HOW TO RUN
--   1. Run as-is  = DRY RUN: the script ends in ROLLBACK, so it verifies the
--      full effect (BEFORE / AFTER result sets) without mutating any data.
--   2. Check the AFTER rows: every rows_with_garble must be 0, and
--      pii_map_526 / pii_map_530 must both read "Rakuten",
--      denylist_has_rocketen = false, denylist_has_rakuten = true.
--   3. Then re-run with the final `ROLLBACK;` changed to `COMMIT;` to apply.
--
-- Plain SQL only (no psql meta-commands), so it can be executed through the
-- Supabase MCP `execute_sql` tool. Everything is scoped to MJ's tenant id
-- 148ccf1c-ef13-47f8-ada1-a98fa90e14a0, inlined literally throughout.
--
-- ----------------------------------------------------------------------------
-- MECHANISM (root cause = iOS on-device speech-to-text, not the PII layer)
-- ----------------------------------------------------------------------------
-- On 2026-07-03 MJ sent a voice note about a work meeting at *Rakuten* (楽天)
-- from the iOS app. iOS voice input is transcribed ON DEVICE by Apple's speech
-- recognizer and POSTed as text to the chat ingress
-- (apps/router/chat_views.py — text only; no server-side audio path exists for
-- iOS, and the two server-side Whisper sites in apps/router/poller.py /
-- apps/router/line_webhook.py serve only Telegram/LINE). The recognizer decoded
-- the brand as "Rocketen". Proof: pending_messages 41abd651 (channel=ios,
-- 2026-07-03 15:19 JST) stores the received text "...a meeting with my team at
-- Rocketen...".
--
-- That literal token was written into the day's journal + chat, then:
--   * The daily-summary carry-forward re-quoted it into later daily notes.
--   * memory_sync redacted the note for the file share; NER mislabeled the
--     unknown proper noun as PERSON (COMPANY_NAME is intentionally NOT detected
--     — apps/pii/config.py) and froze it into Tenant.pii_entity_map as
--     [PERSON_526]="rocketen" (+ a later [PERSON_530]="Rocket").
-- The PII redactor uses EXACT canonical-key matching (casefold+strip), so the
-- correct "Rakuten" never collided onto the garble — the map faithfully mirrors
-- the misheard surface forms; it did not create them and does not corrupt
-- correct mentions. The garble persists purely because it is baked into stored
-- text (and the two mirror map rows).
--
-- Faucet fixes (separate from this script):
--   * iOS (the incident channel): the app feeds the tenant vocabulary from
--     GET /api/v1/chat/transcription-vocab/ into Apple's
--     SFSpeechRecognitionRequest.contextualStrings — landing in nbhd-ios.
--   * Telegram/LINE (server-side Whisper): same vocabulary passed as the
--     Whisper `prompt` (this PR). "rakuten" is on MJ's denylist, so every
--     channel is now biased toward the correct spelling.
--
-- ----------------------------------------------------------------------------
-- EXPECTED EFFECT (tenant 148ccf1c-ef13-47f8-ada1-a98fa90e14a0 ONLY)
-- ----------------------------------------------------------------------------
-- Whole-word, case-insensitive  "Rocketen" -> "Rakuten"  in stored text:
--   journal_document.markdown ............ 3 rows / 5 occurrences (daily notes
--                                          2026-07-03/04/05)
--   journal_document_chunks.text ......... 2 rows / 2 occurrences (search index
--                                          of the notes; re-embed after — below)
--   app_chat_messages.user_text/reply_text 3 rows / 5 occurrences (iOS history)
--   proactive_outbounds.message_text ..... 2 rows / 2 occurrences (assistant
--                                          outbound that quoted the goal)
--   lessons.context ...................... 1 row  (id 879)
--   pending_messages.user_text ........... 1 row  (41abd651 — the received text)
-- pii_entity_map: repair the two mirror rows' SURFACE FORM (rows kept, not
--   deleted, because MJ's own correction messages in pending_messages still
--   reference the placeholders — deleting would orphan them to a raw
--   "[PERSON_526]"). Shape-aware: entries may be {"name": ...} objects or
--   legacy bare strings; both shapes are handled (an object keeps its other
--   keys, e.g. arbiter_judged_at; a bare string is replaced in place):
--     [PERSON_526]  "rocketen" -> "Rakuten"
--     [PERSON_530]  "Rocket"   -> "Rakuten"
-- pii_denylist: drop the two garble keys so the transcription vocabulary never
--   feeds them back (keep the legitimate brand keys "rakuten" / "rakuten cc"):
--     remove "rocketen", "rocket"
-- Legitimate Rakuten data is left untouched: [CREDIT_CARD_14]="Rakuten",
--   [PERSON_387]/[PERSON_90]="Rakuten CC(*)", the rakuten.co.jp order emails,
--   and the 30+ correct "Rakuten" mentions across the journal.
--
-- ----------------------------------------------------------------------------
-- POST-REPAIR OPERATIONAL STEPS (NOT SQL — run after COMMIT):
--   1. Re-sync the file-share mirror so the container stops reading the stale
--      redacted note:  apps.journal.tasks.sync_documents_to_workspace(tenant_id)
--   2. Re-embed the corrected journal_document rows so vector search stops
--      surfacing "Rocketen" (chunk *text* is fixed here; the *embedding*
--      refreshes on the next document re-chunk).
-- ============================================================================

BEGIN;

-- ---- BEFORE -----------------------------------------------------------------
SELECT 'BEFORE' AS phase, 'journal_document' AS tbl,
       count(*) FILTER (WHERE markdown ~* '\yrocketen\y') AS rows_with_garble
FROM journal_document WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'BEFORE', 'app_chat_messages',
       count(*) FILTER (WHERE (coalesce(user_text, '') || coalesce(reply_text, '')) ~* '\yrocketen\y')
FROM app_chat_messages WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'BEFORE', 'journal_document_chunks',
       count(*) FILTER (WHERE text ~* '\yrocketen\y')
FROM journal_document_chunks WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'BEFORE', 'proactive_outbounds',
       count(*) FILTER (WHERE message_text ~* '\yrocketen\y')
FROM proactive_outbounds WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'BEFORE', 'lessons',
       count(*) FILTER (WHERE context ~* '\yrocketen\y')
FROM lessons WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'BEFORE', 'pending_messages',
       count(*) FILTER (WHERE user_text ~* '\yrocketen\y')
FROM pending_messages WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0';

-- ---- TEXT REPAIRS (whole word, case-insensitive) ----------------------------
UPDATE journal_document
   SET markdown = regexp_replace(markdown, '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
   AND markdown ~* '\yrocketen\y';

UPDATE journal_document_chunks
   SET text = regexp_replace(text, '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
   AND text ~* '\yrocketen\y';

UPDATE app_chat_messages
   SET user_text  = regexp_replace(coalesce(user_text, ''),  '\yrocketen\y', 'Rakuten', 'gi'),
       reply_text = regexp_replace(coalesce(reply_text, ''), '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
   AND (coalesce(user_text, '') || coalesce(reply_text, '')) ~* '\yrocketen\y';

UPDATE proactive_outbounds
   SET message_text = regexp_replace(message_text, '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
   AND message_text ~* '\yrocketen\y';

UPDATE lessons
   SET context = regexp_replace(context, '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
   AND context ~* '\yrocketen\y';

-- Historical record of the received (on-device-transcribed) text. Rewritten for
-- consistency so the iOS scrollback reads what MJ meant; drop this statement if
-- you prefer to keep the delivery queue byte-for-byte as received.
UPDATE pending_messages
   SET user_text = regexp_replace(user_text, '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
   AND user_text ~* '\yrocketen\y';

-- ---- PII MAP: repair the two mirror rows' surface form (rows KEPT) ----------
-- Shape-aware: {"name": ...} entries get only their name replaced (other keys
-- like arbiter_judged_at preserved); legacy bare-string entries are replaced in
-- place as strings. Two independent statements so neither key's presence gates
-- the other.
UPDATE tenants
   SET pii_entity_map =
       CASE jsonb_typeof(pii_entity_map -> '[PERSON_526]')
           WHEN 'object' THEN jsonb_set(pii_entity_map, '{[PERSON_526],name}', '"Rakuten"'::jsonb, false)
           ELSE jsonb_set(pii_entity_map, '{[PERSON_526]}', '"Rakuten"'::jsonb, false)
       END
 WHERE id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
   AND pii_entity_map ? '[PERSON_526]';

UPDATE tenants
   SET pii_entity_map =
       CASE jsonb_typeof(pii_entity_map -> '[PERSON_530]')
           WHEN 'object' THEN jsonb_set(pii_entity_map, '{[PERSON_530],name}', '"Rakuten"'::jsonb, false)
           ELSE jsonb_set(pii_entity_map, '{[PERSON_530]}', '"Rakuten"'::jsonb, false)
       END
 WHERE id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
   AND pii_entity_map ? '[PERSON_530]';

-- ---- PII DENYLIST: drop the garble keys, keep the real brand ----------------
UPDATE tenants
   SET pii_denylist = pii_denylist - 'rocketen' - 'rocket'
 WHERE id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0';

-- ---- AFTER (all rows_with_garble must read 0) -------------------------------
SELECT 'AFTER' AS phase, 'journal_document' AS tbl,
       count(*) FILTER (WHERE markdown ~* '\yrocketen\y') AS rows_with_garble
FROM journal_document WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'AFTER', 'app_chat_messages',
       count(*) FILTER (WHERE (coalesce(user_text, '') || coalesce(reply_text, '')) ~* '\yrocketen\y')
FROM app_chat_messages WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'AFTER', 'journal_document_chunks',
       count(*) FILTER (WHERE text ~* '\yrocketen\y')
FROM journal_document_chunks WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'AFTER', 'proactive_outbounds',
       count(*) FILTER (WHERE message_text ~* '\yrocketen\y')
FROM proactive_outbounds WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'AFTER', 'lessons',
       count(*) FILTER (WHERE context ~* '\yrocketen\y')
FROM lessons WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'AFTER', 'pending_messages',
       count(*) FILTER (WHERE user_text ~* '\yrocketen\y')
FROM pending_messages WHERE tenant_id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0';

-- Map values read shape-aware (object name, else bare string). Expect both
-- pii_map values = Rakuten; denylist_has_rocketen/rocket = false; the
-- legitimate brand keys stay (denylist_has_rakuten = true).
SELECT 'AFTER' AS phase, 'pii_map_526' AS what,
       coalesce(pii_entity_map #>> '{[PERSON_526],name}', pii_entity_map ->> '[PERSON_526]') AS value
FROM tenants WHERE id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'AFTER', 'pii_map_530',
       coalesce(pii_entity_map #>> '{[PERSON_530],name}', pii_entity_map ->> '[PERSON_530]')
FROM tenants WHERE id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'AFTER', 'denylist_has_rocketen', (pii_denylist ? 'rocketen')::text
FROM tenants WHERE id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'AFTER', 'denylist_has_rocket', (pii_denylist ? 'rocket')::text
FROM tenants WHERE id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'
UNION ALL SELECT 'AFTER', 'denylist_has_rakuten', (pii_denylist ? 'rakuten')::text
FROM tenants WHERE id = '148ccf1c-ef13-47f8-ada1-a98fa90e14a0';

-- DRY RUN by default. After review: change ROLLBACK; to COMMIT; and re-run.
ROLLBACK;
