# Symlink + rename interaction — UNRESOLVED, verify empirically

## The question

The handoff plan symlinks `~/.claude/.credentials.json` → `/home/node/.openclaw/claude-state/.credentials.json` (file share). This persists the binary's in-place refresh writes across container revisions.

**It works iff** the `claude` binary updates the credentials file via either:
- `fs.writeFile(path, ...)` (writes through symlink to target — symlink intact), OR
- `fs.writeFileSync(path, ...)` (same), OR
- `fs.open + fs.write + fs.close` on the path (same)

**It fails silently iff** the binary uses an atomic-write pattern:
- `fs.writeFile(tmpPath, ...)` then `fs.rename(tmpPath, finalPath)` — `rename` REPLACES the symlink at `finalPath` with a regular file. Subsequent refreshes write to the writable container layer; the share copy stops updating. Next revision boots from the (now stale) seed in KV.

## Research findings

Could not definitively determine the write pattern from binary strings alone. The Claude Code 2.1.123 native binary at `/tmp/cc-pack/native/package/claude` (215 MB Mach-O) embeds the Node.js runtime which exposes BOTH `fs.writeFile` and `fs.renameSync` — both symbols are present, neither is conclusive evidence of which path the credentials code uses.

Signals from `strings`:
- `mkdtemp` and `tmpdir` symbols exist but are Node stdlib defaults — used by anything that happens to want a temp dir
- `tengu_oauth_refresh_token_cleared_invalid_grant` confirms in-process refresh (good — the binary does write back)
- No `tmp.credentials` or `.credentials.tmp` pattern found in strings (mild signal AGAINST atomic-write, but not conclusive — pattern could be in-memory)

The JS source for Claude Code is not in the npm package (`/tmp/cc-pack/package/` only contains `cli-wrapper.cjs` and `install.cjs`; the actual JS is bundled into the native binary).

## Verification plan for Phase 2 implementation

Before relying on the symlink approach in production, run this empirical test in the canary container `oc-148ccf1c-ef13-47f8-a` (or any non-prod tenant):

1. Set up symlink: `~/.claude/.credentials.json` → `/home/node/.openclaw/claude-state/.credentials.json`
2. Write a valid OAuth credentials file at the target path
3. Trigger a refresh — easiest: set `expiresAt` to a time ~1 minute in the future, then make a `claude --print "hello"` call after the expiry. The binary should refresh.
4. After refresh, run:
   ```sh
   ls -la ~/.claude/.credentials.json /home/node/.openclaw/claude-state/.credentials.json
   stat -c '%N %i' ~/.claude/.credentials.json /home/node/.openclaw/claude-state/.credentials.json
   ```
5. **Pass case**: `~/.claude/.credentials.json` is still a symlink; both paths point to the same inode; share file has the new `accessToken`.
6. **Fail case**: `~/.claude/.credentials.json` is now a regular file (different inode); share file unchanged. Symlink approach is broken — fall back to alternative below.

## Alternative if the symlink breaks

If empirical test fails (binary uses rename-replace), the fallback is a **bind mount** at the file level. Approaches in order of preference:

1. **Mount the share directly at `/home/node/.claude/`** instead of using a symlink. The whole `~/.claude/` directory becomes file-share-backed. Trade-off: also persists `~/.claude/projects/` (already symlinked there today), settings.json, and any other claude state — harmless or beneficial.
2. **Polling sync sidecar**: a small node process watches `~/.claude/.credentials.json` (via `fs.watch`) and, on change, copies to `/home/node/.openclaw/claude-state/.credentials.json`. Survives rename-replace because the watcher rewatches.
3. **Per-turn check + copy**: at the start of every agent turn (via an OpenClaw lifecycle hook if available, or a wrapper around the binary spawn), copy the writable-layer file to the share. Cheap but fragile.

Option 1 is cleanest and matches what the original brief proposed; the only reason we didn't go straight there was preserving the existing share-mount layout. Worth re-evaluating if the symlink fails.

## Bottom line

**Don't ship the symlink approach without running the test above.** If it fails, switch to option 1 (whole-directory mount) — same security properties, slightly more storage on the share, no surprises.
