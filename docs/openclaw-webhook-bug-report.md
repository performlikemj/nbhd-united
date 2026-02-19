# [Bug]: Telegram webhook crash loop — unhandled rejection + EADDRINUSE on restart

## Summary

Telegram webhook mode crashes immediately after successful startup on hosts with unreliable/slow IPv6. The webhook binds, `setWebhook` succeeds, but ~2ms later an auto-restart triggers. The restart attempts to rebind the same port without closing the previous server, causing `EADDRINUSE` and a process crash.

Polling mode works perfectly on the same host.

## Environment

- **OpenClaw:** 2026.2.17 (also reproduced on 2026.2.13, 2026.2.15)
- **Node.js:** 22 (bookworm-slim)
- **Host:** Docker container on a network where IPv6 to `api.telegram.org` times out

## Reproduction

1. Run OpenClaw on a host where `curl -6 https://api.telegram.org` times out (IPv4 works fine)
2. Enable Telegram webhook mode with `network.autoSelectFamily: false`
3. Container starts, webhook binds, then immediately crash-loops

## Logs

```
[telegram] autoSelectFamily=false (config)
[telegram] [default] starting provider (@BotName)
[telegram] webhook listening on https://example.com/webhook/
[telegram] [default] auto-restart attempt 1/10 in 5s        ← 2ms after "webhook listening"
[telegram] [default] starting provider (@BotName)            ← retry
[openclaw] Uncaught exception: Error: listen EADDRINUSE :::8787
```

## Three bugs

### 1. `network.autoSelectFamily` doesn't apply to all outbound calls

The config takes effect (confirmed in logs), and the grammY bot client respects it. But something during webhook startup — likely `setMyCommands`, `getMe`, or another fire-and-forget API call — uses a different HTTP client or native `fetch` that doesn't inherit the setting. That call fails on IPv6 and triggers the crash.

Setting `--dns-result-order=ipv4first` and `--no-network-family-autoselection` in `NODE_OPTIONS` also didn't prevent the crash, which suggests the failing call may override these at the request level.

### 2. Unhandled promise rejection from fire-and-forget calls

The network failure produces an unhandled rejection that causes the provider task to exit. The gateway interprets this as the provider crashing and triggers a restart. A failed `setMyCommands` (or similar non-critical call) should not bring down the entire gateway.

### 3. Port not released before restart

When the auto-restart fires, the previous webhook HTTP server on port 8787 is still listening. The restart creates a new server on the same port → `EADDRINUSE`. The cleanup/restart path needs to `server.close()` and wait for it before rebinding.

## What we tested

| Attempt | Result |
|---|---|
| `autoSelectFamily: false` in config | ❌ Crash (config confirmed applied) |
| `--dns-result-order=ipv4first` in NODE_OPTIONS | ❌ Crash |
| `--no-network-family-autoselection` in NODE_OPTIONS | ❌ Crash |
| `commands.native: false`, `commands.nativeSkills: false` | ❌ Crash |
| `deleteWebhook` first (clean Telegram state) | ❌ Crash |
| Pinned to 2026.2.13, 2026.2.15 | ❌ Crash |
| Telegram disabled | ✅ Stable |
| Polling mode (no `webhookUrl`) | ✅ Stable |

## Similar report

- [Reddit: Gateway crash loop when enabling Telegram provider](https://www.reddit.com/r/LocalLLaMA/comments/1qn8faz/) — same symptoms, different host, also not fixed by `--dns-result-order=ipv4first`

## Workaround

Use polling mode (omit `webhookUrl` from config). Call `deleteWebhook` on the Telegram bot API first if an old webhook is registered.

## Suggested fixes

1. Route all Telegram API calls through the same HTTP client that respects `network.autoSelectFamily`
2. Catch errors from non-critical startup calls (`setMyCommands`, etc.) so they don't crash the provider
3. Call `server.close()` on the existing webhook listener before attempting to rebind on restart
