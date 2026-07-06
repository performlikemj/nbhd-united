-- ============================================================================
-- DATA REPAIR — MJ tenant "Rakuten" → "Rocketen" voice-transcription garble
-- ============================================================================
--
--   DO NOT RUN AGAINST PRODUCTION UNTIL REVIEWED AND APPROVED.
--
-- This script is committed for review only. As written it ends in ROLLBACK, so
-- executing it verifies the effect WITHOUT mutating any data. To actually apply
-- the repair, change the final `ROLLBACK;` to `COMMIT;` after review.
--
-- ----------------------------------------------------------------------------
-- MECHANISM (root cause = speech-to-text, not the PII layer)
-- ----------------------------------------------------------------------------
-- On 2026-07-03 MJ sent a voice note about a work meeting at *Rakuten* (楽天).
-- Transcription (OpenAI Whisper `whisper-1`, called with NO vocabulary prompt —
-- see apps/router/poller.py / apps/router/line_webhook.py) misheard the brand
-- as "Rocketen". Proof: pending_messages 41abd651 (ios, 2026-07-03 15:19 JST)
-- stores the raw transcript "...a meeting with my team at Rocketen...".
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
-- Faucet fix (separate, in this same PR): apps/router/transcription.py adds a
-- per-tenant Whisper vocabulary hint. "rakuten" is now on MJ's denylist, so the
-- hint will bias future transcription to "Rakuten".
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
--   pending_messages.user_text ........... 1 row  (41abd651 — the transcript)
-- pii_entity_map: repair the two mirror rows' surface form (kept, not deleted,
--   because MJ's own correction messages in pending_messages still reference the
--   placeholders — deleting would orphan them to a raw "[PERSON_526]"):
--     [PERSON_526].name "rocketen" -> "Rakuten"
--     [PERSON_530].name "Rocket"   -> "Rakuten"
-- pii_denylist: drop the two garble keys so the transcription hint never feeds
--   them back (keep the legitimate brand keys "rakuten" / "rakuten cc"):
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
--      surfacing "Rocketen" (chunk *text* is fixed here; the *embedding* refreshes
--      on the next document re-chunk).
-- ============================================================================

BEGIN;

\set tid '148ccf1c-ef13-47f8-ada1-a98fa90e14a0'

-- ---- BEFORE -----------------------------------------------------------------
SELECT 'BEFORE' AS phase, 'journal_document' AS tbl,
       count(*) FILTER (WHERE markdown ~* '\yrocketen\y') AS rows_with_garble
FROM journal_document WHERE tenant_id = :'tid'
UNION ALL SELECT 'BEFORE','app_chat_messages',
       count(*) FILTER (WHERE (coalesce(user_text,'')||coalesce(reply_text,'')) ~* '\yrocketen\y')
FROM app_chat_messages WHERE tenant_id = :'tid'
UNION ALL SELECT 'BEFORE','journal_document_chunks',
       count(*) FILTER (WHERE text ~* '\yrocketen\y') FROM journal_document_chunks WHERE tenant_id = :'tid'
UNION ALL SELECT 'BEFORE','proactive_outbounds',
       count(*) FILTER (WHERE message_text ~* '\yrocketen\y') FROM proactive_outbounds WHERE tenant_id = :'tid'
UNION ALL SELECT 'BEFORE','lessons',
       count(*) FILTER (WHERE context ~* '\yrocketen\y') FROM lessons WHERE tenant_id = :'tid'
UNION ALL SELECT 'BEFORE','pending_messages',
       count(*) FILTER (WHERE user_text ~* '\yrocketen\y') FROM pending_messages WHERE tenant_id = :'tid';

-- ---- TEXT REPAIRS (whole word, case-insensitive) ----------------------------
UPDATE journal_document
   SET markdown = regexp_replace(markdown, '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = :'tid' AND markdown ~* '\yrocketen\y';

UPDATE journal_document_chunks
   SET text = regexp_replace(text, '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = :'tid' AND text ~* '\yrocketen\y';

UPDATE app_chat_messages
   SET user_text  = regexp_replace(coalesce(user_text, ''),  '\yrocketen\y', 'Rakuten', 'gi'),
       reply_text = regexp_replace(coalesce(reply_text, ''), '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = :'tid'
   AND (coalesce(user_text, '') || coalesce(reply_text, '')) ~* '\yrocketen\y';

UPDATE proactive_outbounds
   SET message_text = regexp_replace(message_text, '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = :'tid' AND message_text ~* '\yrocketen\y';

UPDATE lessons
   SET context = regexp_replace(context, '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = :'tid' AND context ~* '\yrocketen\y';

-- Historical record of MJ's actual transcript. Rewritten for consistency so the
-- iOS scrollback reads what he meant; drop this statement if you prefer to keep
-- the delivery queue byte-for-byte as received.
UPDATE pending_messages
   SET user_text = regexp_replace(user_text, '\yrocketen\y', 'Rakuten', 'gi')
 WHERE tenant_id = :'tid' AND user_text ~* '\yrocketen\y';

-- ---- PII MAP: repair the two mirror rows' surface form (rows KEPT) ----------
UPDATE tenants
   SET pii_entity_map = jsonb_set(
         jsonb_set(pii_entity_map, array['[PERSON_526]','name'], '"Rakuten"'::jsonb, false),
         array['[PERSON_530]','name'], '"Rakuten"'::jsonb, false)
 WHERE id = :'tid'
   AND pii_entity_map ? '[PERSON_526]'
   AND pii_entity_map ? '[PERSON_530]';

-- ---- PII DENYLIST: drop the garble keys, keep the real brand ----------------
UPDATE tenants
   SET pii_denylist = pii_denylist - 'rocketen' - 'rocket'
 WHERE id = :'tid';

-- ---- AFTER (all rows_with_garble must read 0) -------------------------------
SELECT 'AFTER' AS phase, 'journal_document' AS tbl,
       count(*) FILTER (WHERE markdown ~* '\yrocketen\y') AS rows_with_garble
FROM journal_document WHERE tenant_id = :'tid'
UNION ALL SELECT 'AFTER','app_chat_messages',
       count(*) FILTER (WHERE (coalesce(user_text,'')||coalesce(reply_text,'')) ~* '\yrocketen\y')
FROM app_chat_messages WHERE tenant_id = :'tid'
UNION ALL SELECT 'AFTER','journal_document_chunks',
       count(*) FILTER (WHERE text ~* '\yrocketen\y') FROM journal_document_chunks WHERE tenant_id = :'tid'
UNION ALL SELECT 'AFTER','proactive_outbounds',
       count(*) FILTER (WHERE message_text ~* '\yrocketen\y') FROM proactive_outbounds WHERE tenant_id = :'tid'
UNION ALL SELECT 'AFTER','lessons',
       count(*) FILTER (WHERE context ~* '\yrocketen\y') FROM lessons WHERE tenant_id = :'tid'
UNION ALL SELECT 'AFTER','pending_messages',
       count(*) FILTER (WHERE user_text ~* '\yrocketen\y') FROM pending_messages WHERE tenant_id = :'tid';

SELECT 'AFTER' AS phase, 'pii_map_526' AS what, pii_entity_map #>> '{[PERSON_526],name}' AS value FROM tenants WHERE id = :'tid'
UNION ALL SELECT 'AFTER','pii_map_530', pii_entity_map #>> '{[PERSON_530],name}' FROM tenants WHERE id = :'tid'
UNION ALL SELECT 'AFTER','denylist_has_rocketen', (pii_denylist ? 'rocketen')::text FROM tenants WHERE id = :'tid'
UNION ALL SELECT 'AFTER','denylist_has_rakuten', (pii_denylist ? 'rakuten')::text FROM tenants WHERE id = :'tid';

-- Change to COMMIT; after review to APPLY. Leaving ROLLBACK makes this a no-op.
ROLLBACK;
