# CodeBuddy Proxy

> A lightweight local proxy that turns CodeBuddy's underlying chat interface into standard **OpenAI Chat Completions**, **Responses**, and **Anthropic Messages** protocols — so you can plug CodeBuddy models into Codex CLI, Claude Code / CC Switch, OpenCode, Grok, Oh My Pi and any OpenAI-compatible client.

> **中文文档见 [README_zh.md](README_zh.md).**

---

## Features

- **Protocol conversion** — `/v1/chat/completions` (OpenAI), `/v1/responses` (Codex CLI), `/v1/messages` (Anthropic / Claude Code)
- **Admin UI** — built-in web console at `/ui`: browse models grouped by provider, one-click "set as default model", one-click test per model (sends a "hi"), and per-model request stats with charts. Loads lazily per tab, so the first paint never waits on the slowest endpoint (the request log)
- **Model list** — `/v1/models` returns an OpenAI-compatible model list plus rich per-model metadata (context window, credits, input modalities / image support)
- **Desensitization** (`--desensitize`) — inserts zero-width spaces into compliance terms inside system messages to avoid backend false-blocking by keyword review
- **Message compression** (`--optimize-context`) — compresses long histories / large schemas / oversized tool output for `/v1/responses`, cutting token usage dramatically
- **Tool calls** — full function calling support with automatic filtering of invalid tool definitions; `tool_choice` is normalized across both OpenAI and Anthropic shapes so it never reaches the upstream as an object
- **DSML parsing** — detects and converts DeepSeek Markup Language tool calls
- **Streaming** — SSE output with idle / total-duration timeout protection
- **Multi-account** — isolated session files for work / personal accounts
- **Multi-provider** — besides CodeBuddy, built-in **Trae** (decrypts the Trae IDE login, connects straight to the underlying models), **ZCode** (Zhipu GLM), **Doubao** (pure-stdlib CDP into the Doubao desktop app) **Xiaomi MiMo** (API key, or reuses the MiMo Desktop Xiaomi-account login) and **Qoder** (COSY signing reimplemented in pure Python — Qwen3.8 / GLM / Kimi); all listed by `/v1/models` and routed by model name
- **Both wire protocols** — OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`, i.e. Claude Code) over the same models; each provider converts responses back to whichever protocol the client asked for

---

## Install & run

The proxy is a plain Python package under `src/`. Run from source with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run python -m buddy_proxy --desensitize
```

The first run creates the state directory `~/.buddy-proxy/` (mode `0700`; override with
`BUDDY_PROXY_STATE_DIR`). Everything machine-local lives there: `settings.json` (default
model, disabled models, model time windows), the Trae Work credential `trae_work.json`,
the PAT token cache `trae_pat_token.json`, the cached MiMo token/serviceToken state, and
the client-name map `buddy_client_names.json`. It holds credentials — keep it out of
backups and version control. Startup prints the resolved path as `[State] ...`.

### The `buddy` command (recommended)

`buddy` is the day-to-day entry point: one command starts the proxy and opens the admin UI. It can also register the proxy as a macOS launchd service (auto-start at login, automatic restart on crash).

```bash
./buddy start              # start (if not running) and open http://127.0.0.1:8787/ui
./buddy stop / restart / status / logs
./buddy login [provider]   # upstream login (codebuddy(=workbuddy)/trae/zcode/doubao/mimo/qoder)
./buddy ui                 # just open the admin UI (starts the proxy if needed)
./buddy update             # update to the latest code (git pull -> uv sync -> restart)

# one-time install: put buddy on your PATH so it works from anywhere
./buddy install            # -> /usr/local/bin (falls back to ~/.local/bin)

# register as a system service (launchd)
./buddy service install    # start/stop/restart now route through launchctl
./buddy service status
./buddy service uninstall
```

- When the service is installed, `buddy start/stop/restart` automatically use `launchctl`; otherwise they fall back to `proxy.sh`'s pid management.
- Env vars `PROXY_HOST` (default `0.0.0.0`), `PROXY_PORT` (default `8787`) and `PROXY_EXTRA_ARGS` apply to both `start` and `service install`.
- `buddy update` runs `git pull --ff-only` in the repo it was installed from, then `uv sync`, then restarts the service. It **refuses to run when the working tree has uncommitted changes** — it will not stash, merge or otherwise touch work in progress, so a half-finished edit can never be silently overwritten. The refusal prints the offending files and a hint tailored to what is actually dirty: if any untracked file is present it suggests `git stash -u` (plain `git stash` leaves untracked files behind, so following that advice would land you right back on the same refusal), otherwise plain `git stash`. `--ff-only` means a diverged branch fails loudly instead of creating a surprise merge commit — the failure message quotes git's own wording (`Not possible to fast-forward` vs `Could not read from remote`) so you can tell a diverged branch from an unreachable remote. If `uv sync` fails the service is left running rather than restarted onto broken dependencies. Running it on an already-current checkout is harmless: it just re-syncs dependencies and restarts.

### The `proxy.sh` management script

`buddy` calls `proxy.sh` internally; you can drive it directly for finer control:

```bash
./proxy.sh start          # background start, returns immediately
./proxy.sh stop           # stop the running instance
./proxy.sh restart        # restart (with the same args)
./proxy.sh status         # show PID and listening address
./proxy.sh logs           # tail -F the log file
./proxy.sh ui             # ensure it's running, then open the admin UI
./proxy.sh login codebuddy  # upstream login (also: trae / zcode / doubao / mimo / qoder)

# customize host / port / args
./proxy.sh start -p 9000 -H 0.0.0.0
PROXY_PORT=9000 PROXY_EXTRA_ARGS="--desensitize --optimize-context" ./proxy.sh start
```

The script:
- Detects `.venv/bin/python` automatically (preferring the project venv).
- Uses `nohup ... &` so `start` returns immediately — terminal is **not** blocked.
- Writes the PID to `logs/proxy.pid` and startup output to `logs/proxy.sh.log`; app logs rotate daily (`logs/proxy.log` + `logs/buddy-proxy.jsonl`, 30 days kept).
- Stops cleanly with `kill`; falls back to `kill -9` after 10s if the process doesn't exit.

---

## Models

The model catalog is maintained in `src/buddy_proxy/web/models_config.json` — `/v1/models` always serves it (offline-reliable, no remote dependency). The catalog currently ships **46 models** across two channels, each with its credit multiplier (× base cost). `GET /v1/models` → `data[].credits` / `models[].credits` exposes the multiplier:

**CodeBuddy channel** (19) — bare model ids, no prefix:

| id | name | credits |
|---|---|---|
| `auto` | Auto (fast / balanced / ultimate → 0.21 / 0.65 / 1.20) | dynamic |
| `default` | Default | x2.20 |
| `glm-5.3` | GLM-5.3 | x0.79 |
| `glm-5.3-flash` | GLM-5.3-Flash | x0.06 |
| `glm-5.3-flashx` | GLM-5.3-FlashX | x0.14 |
| `glm-5.2` | GLM-5.2 (夜间折扣) | x0.79 |
| `glm-5.1` | GLM-5.1 | x0.79 |
| `glm-5v-turbo` | GLM-5v-Turbo (vision) | x0.71 |
| `hy3` | Hy3 (限时免费) | x0.00 |
| `hy4-preview` | Hy4 preview | x0.29 |
| `minimax-m3` | MiniMax-M3 | x0.25 |
| `kimi-k3` | Kimi-K3 | x1.62 |
| `kimi-k2.8-preview` | Kimi-K2.8-Preview | x0.77 |
| `kimi-k2.7` | Kimi-K2.7-Code | x0.57 |
| `kimi-k2.6` | Kimi-K2.6 | x0.52 |
| `deepseek-v4.1-flash` | Deepseek-V4.1-Flash | x0.11 |
| `space-bunny` | Space-Bunny | x0.03 |
| `deepseek-v4-flash` | Deepseek-V4-Flash | x0.17 |
| `deepseek-v4-pro` | Deepseek-V4-Pro | x0.51 |

> Note on `glm-*`: the bare name resolves to whichever channel claims it first in registration order (**zcode**, which serves `glm-5.3` / `glm-5.3-flash`). For `glm-5.3-flashx` the zcode subscription reports `1311 当前订阅套餐暂未开放GLM-5.3-FlashX权限` while **CodeBuddy serves it fine** — so use the explicit `codebuddy/glm-5.3-flashx` prefix for that one.

**Trae PAT channel** (27) — addresses as `traepat/<id>`. A bare id that several channels declare resolves to whichever one claims it first in registration order (the personal `trae` channel, if enabled) — **not** to CodeBuddy, whose `models()` is empty and is therefore only reached by the no-match fallback or an explicit `codebuddy/` prefix. So always use the `traepat/` prefix when you mean this channel. Credit values here are the channel's own scale:

| id | name | credits |
|---|---|---|
| `gpt-6-astra-max` | GPT-6-Astra Max | — |
| `gpt-5.6-sol-max` / `gpt-5.6-sol` | GPT-5.6-Sol Max / GPT-5.6-Sol | — |
| `gpt-5.6-luna-max` / `gpt-5.6-terra-max` | GPT-5.6-Luna / Terra Max | — |
| `gpt-5.5-max` / `gpt-5.4` / `gpt-5.2` | GPT-5.5 Max / 5.4 / 5.2 | — |
| `gemini-3.1-pro` / `gemini-3-flash` | Gemini-3.1-Pro / Gemini-3-Flash | — |
| `openrouter-3o-max` / `-2o-max` / `-1o` / `-1` | OpenRouter-3o Max / 2o Max / 1o / 1 | — |
| `glm-5.3` / `glm-5.3-flash` | glm-5.3 / glm-5.3-flash | x0.40 / x0.06 |
| `glm-5.2` | glm-5.2 | x0.40 |
| `qwen3.8-max` / `qwen-3.7-plus` | Qwen3.8-Max / Qwen-3.7-Plus | x1.50 / x0.25 |
| `kimi-k3` / `kimi-k2.7-code` / `kimi-k2.6` | kimi-k3 / Kimi-K2.7-Code / Kimi-K2.6 | x1.83 / x0.83 / — |
| `deepseek-v4-pro` / `deepseek-v4-flash` | DeepSeek-V4-Pro / -Flash | x0.72 / x0.08 |
| `minimax-m3` | MiniMax-M3 | x0.26 |
| `Doubao-Seed-2.1-Pro` / `Doubao-Seed-Code` | Seed-2.1-Pro / Seed-Code | x0.77 / x0.03 |

Edit `src/buddy_proxy/web/models_config.json` to add or tweak entries — changes take effect on restart.

First-time login (opens a browser):

```bash
uv run python -m buddy_proxy --login --desensitize
```

It listens on `http://127.0.0.1:8787` by default; the admin UI lives at **http://127.0.0.1:8787/ui** — see [Admin UI](#admin-ui-ui) below.

## Quick check

```bash
curl http://127.0.0.1:8787/health      # service + auth status
curl http://127.0.0.1:8787/v1/models    # model list
```

## Admin UI (`/ui`)

Open <http://127.0.0.1:8787/ui> in a browser (or just run `buddy start` / `buddy ui`):

- **Default model** — models are grouped by provider; click "Set as default" (设为默认) on any model to make it the proxy default. Client requests **without a `model` field** are routed to it automatically. Settings persist in `~/.buddy-proxy/settings.json` (override via `BUDDY_PROXY_SETTINGS`) and survive restarts; `--default-model zcode/glm-5.3` seeds the initial value (an existing settings file wins).
- **One-click test** — every model row has a "Test" (测试) button that sends a real `hi` upstream and shows latency, token usage and the reply preview (non-streaming, `max_tokens=256` — a real, billable upstream call).
- **Stats & charts** — per-provider/per-model request counts, errors, average latency and token usage: a 14-day stacked daily chart, a top-models bar list, and the latest 50 requests. Each completed request appends one line to `logs/metrics.jsonl`; the tail is reloaded on startup so history survives restarts (30 days kept).
- **Provider health** — login/config status at a glance (CodeBuddy session, zcode key, mimo auth mode, ...).
- **Auto check-in & calendar** — CodeBuddy / Trae / Qoder all expose daily sign-in: tick "Auto check-in" (自动打卡) and the proxy claims them every day at the configured time (default 09:30; if the proxy starts later it catches up immediately). The last 35 days are shown as a calendar; "Check in now" (立即打卡) claims manually. Campaigns can be seasonal — when CodeBuddy's is closed the UI shows "no sign-in activity today" and skips it. History is appended to `logs/checkin.jsonl`. ZCode (GLM Coding Plan) / Doubao have no sign-in API.
- **Next check-in time per channel** — each row shows when the next claim opens, with a per-second countdown (computed locally — no upstream calls; it stops when you leave the tab or the page is hidden). The three channels **rotate on different schedules**, so the source of the timestamp differs: Qoder's window is `10:00 → 09:59` next day (the upstream returns `startAt`/`endAt`, marked `upstream`), while CodeBuddy and Trae rotate at **local midnight** — neither upstream reports a daily rotation field at all (CodeBuddy only gives the whole campaign season, Trae no time fields whatsoever), so midnight is inferred from the real claim timestamps in `logs/checkin.jsonl`, marked `inferred` and given a dashed border plus an italic timestamp in the UI (a marker on the value itself reads as a typo). While a Qoder claim is still unclaimed the timestamp shown is the **deadline** for this round (miss it and it's gone), not the next round's start. When no next time can be computed (campaign over, season ended, nothing today) it is simply not shown rather than showing a stale timestamp. Once the countdown reaches "即将刷新" the next poll picks up the new state **immediately** — that same moment doubles as the expiry condition for the cached status snapshot (otherwise, with the rotation falling inside the cache's 5-minute TTL, the UI would keep showing "已签到" and a disabled button, and those five minutes are enough for Qoder to lose a whole round). To keep an upstream that keeps returning a long-past value from wedging the cache, this early expiry only applies within a one-hour grace window.
- **"Already checked in today" defers to upstream** — local history dedupes by **calendar day**, but Qoder rotates on a 10:00 window, so the two disagree: an early-morning poll records the *previous* round's `checked_in` as a claim for today, and when the new campaign opens at 10:00 and the upstream says `claimable`, the old logic still skipped it for the whole day while the UI showed "已签到" with the button disabled. Now anything the upstream reports as claimable is claimed, and the button is no longer wrongly disabled; the midnight-rotating CodeBuddy / Trae are unaffected.
- **Quota** — remaining allowance per provider at a glance: CodeBuddy credit packs (total remaining + per-pack detail), Trae total allowance + entitlement pack expiry, ZCode 5-hour / monthly windows with reset times, MiMo weekly quota + plan validity. CodeBuddy's pack detail is capped at 4 rows for display, but its "total remaining" figures are always accumulated over **every** pack — the cap never truncates the sum. Each card's headline is driven by an explicit `sum_items` flag from the provider (the panel headline adds the buckets up when set, otherwise takes the first item), because the per-item rows are not comparable across channels: Qoder's subscription quota + add-on pack + dedicated credits are genuinely coexisting allowances whose sum is the account total (the upstream's own `totalUsagePercentage` is computed the same way), whereas Trae's entitlement packs are a *breakdown* of the total allowance it already reports (adding them double-counts) and ZCode / MiMo report different units (5-hour window vs monthly window; percent vs days). Providers that can be summed must therefore opt in; the default is off so a new channel is never silently mis-added. Quota responses are cached for 5 minutes. The Trae PAT standard pool has no active query API — its usage is collected **passively** from the `4031` (quota exhausted) error body, and because that error only fires when the pool is already full, a snapshot that is past its `reset_ts` is shown as "reset · pending confirmation" rather than as a stale 100%. The quota page is the only network-touching endpoint in the admin UI, so its queries are bounded four ways: the gateway is probed for reachability first (DNS+TCP precheck — if unreachable the whole round is skipped and per-account caches are served instead), each account request times out after 6 seconds, accounts are queried concurrently on a shared thread pool, and the whole round is capped at 8 seconds (accounts that miss it fall back to their caches while their requests finish in the background) so latency does not grow with the account count. When the gateway is unreachable or some accounts fail, the page shows an explicit "n/m accounts got no fresh data" notice in a warning colour instead of spinning indefinitely; such failures are cached for only 30 seconds (successes keep the 5-minute cache) so a brief network blip self-heals on the next round.
- **Trae PAT accounts** — per-account cards showing local credential and cooldown state (read purely locally, never touching the network), one-click token refresh that only fills in missing/expiring tokens, and the upstream's per-model load status for the PAT channel (cached for 10 minutes).
- **Model toggle & schedule** — disable/enable an individual `(provider, model)` pair (a disabled pair fails fast), and restrict one to time windows such as `22:00–08:00` or `12:00–14:00`. Both persist to the settings file.
- **Model order (candidate failover)** — its own **Model order** tab shows **one card per key in `model_order`** — whatever the config lists is what the page shows, nothing expanded from the channel catalog. Each card expands to an interactive list of candidate upstreams you can add to, delete from, or **drag / ▲▼** to reorder. Requests try them top-down: when a channel fails **before anything was written to the client**, the proxy moves to the next one and marks the failed target with a 5-minute cooldown (extended to 1 hour on repeated failures), skipping it while it lasts. One failure is deliberately **not** marked: a **local DNS resolution failure**. That is your machine's problem, not the upstream's — it hits every candidate at once, so cooling them all would turn a one-second blip into five minutes of "model unavailable" with not even a re-probe, which is exactly what happened on 2026-10-02. Such a failure still moves on to the next candidate, it just leaves no mark. Once streaming has started there is no failover (the upstream would bill twice) — so CodeBuddy's in-stream errors never trigger it, and putting CodeBuddy last is the practical choice; those in-stream errors are at least recorded as failures in the request log rather than counted as a 200. The key is the **bare model name** (`deepseek-v4.1-flash`): it means "when this model is requested, try these channels in this order, the first by default", so one entry covers every channel that publishes the name. Targets are written as `provider/model`. An empty list restores the historical behaviour (route purely by model id). Persists to `model_order` in the settings file. A **clear-cooldown** button on the model row retries cooled targets immediately without touching the order.
- **Settings health banner** — `load_settings()` swallows a corrupt settings file by design (one bad comma must not take the proxy down), which used to mean *every* settings-backed feature silently vanished at once with nothing to go on. The admin UI now probes the file on load and shows a red banner at the top when it exists but cannot be parsed, quoting the raw JSON error.
- **Request log** — paginated request log read from `logs/metrics.jsonl` plus the 30-day archive, filtered by date range, provider, model and client. The **Credit** column shows the per-request amount when the upstream reports it (CodeBuddy, Qoder, and any channel that fills `usage`), falling back to a `≈` estimate — for trae, measured per-token input/output rates (`MEASURED_CREDIT_RATES`, reconciled against the official usage records) scaled by `current multiplier / calibration multiplier` so a `MODEL_CREDITS` edit tracks panel price changes automatically, with the multiplier-based rough estimate for models not yet measured; and the official GLM Coding Plan deduction coefficients (with cache-hit and time-of-day discounts) for zcode — and `—` when neither exists. Shown to 2 decimals like Qoder's own site; the raw value stays in `logs/metrics.jsonl`. Note **cached tokens are nearly free**: a 164k-input request that hit ~99% cache cost 0.12 credits, while a 12k-input request with no cache cost 0.31 — so a large input column does not imply a large bill.

The admin UI also lets you switch the **fallback provider** (used when a request matches no model); it persists in the same settings file.

Tabs load lazily and load in parallel on demand, and each tab shows a skeleton until its own data arrives — opening the UI no longer waits on the request log, which is the slowest endpoint because it reads and parses the whole JSONL tail on every poll. Manual refresh invalidates in-flight requests so a late-arriving old response can never overwrite a newer one.

Security: the `/ui/api/*` admin endpoints are **restricted to localhost (127.0.0.1)**. To manage the proxy from the LAN, set `BUDDY_PROXY_ADMIN_OPEN=1` (at your own risk). `/v1/*` proxy endpoints are unaffected.

## Doubao provider (optional)

A built-in provider that drives the local Doubao desktop app (DoubaoWork.app) via the Chrome DevTools Protocol — it reuses the app's own login state and injects risk-control signatures inside the page's JS context. Pure stdlib, no Playwright. Enable with `--doubao`.

> **Key rule: let the proxy launch Doubao — don't open the app yourself first.**
> On the first `doubao` request (or a "Test" click in the admin UI) the proxy automatically
> starts the app with a CDP debug port (9223), connects to its embedded browser and reuses
> your login. If Doubao is already running without that port, the proxy refuses to kill
> your app and the first request fails with 502 "主 App 正在运行但未开启 CDP 调试端口" —
> fully quit Doubao (Cmd+Q) and retry; the proxy then launches it correctly.

| Symptom | Cause | Fix |
| --- | --- | --- |
| 502 "…未开启 CDP 调试端口" | Doubao was started before the proxy | Quit Doubao (Cmd+Q) and retry — the proxy relaunches it |
| requests fail after a Doubao update/restart | stale CDP connection | Quit Doubao and retry, or `buddy restart` |
| 401 "doubao not logged in" | login expired | sign in inside the Doubao app, retry |

Models: classic pipeline `doubao` / `doubao-think` / `doubao-expert` (the server routes to its default model — the `model` field is ignored) and agent pipeline `doubao-auto`, `doubao-2.1-turbo`, `doubao-2.1-pro`, `orange-5.0`, `gemini-3.7-flash`, `gpt-5.6-sol` (real per-model routing via the app's own model menu; optional `reasoning_effort` 3–7).

## Trae provider (optional)

Decrypts the Trae IDE's locally stored login state and talks straight to the underlying models.

```bash
uv run python -m buddy_proxy --desensitize --trae
```

- **How it works** — decrypts the AES-128-CBC + SHA-512 `tc` blob in the local Trae IDE storage, or reads `TRAE_TOKEN` / `TRAE_USER_ID` from `.env`, then connects to the Trae gateway directly.
- **Native channel** — since 2026-09 all requests (plain chat included) go through the `chat_v3` direct path: with `tools` present it does native function calling (structured `tool_calls` + `role:"tool"` history replay); plain chat gets no server-side agent preset, no suppression instructions and no leak scrubbing. It also returns real token usage. Set `WB_TRAE_NATIVE_TOOLS=0` to fall back to the legacy prompt-taught text protocol.
- **PAT channel** — `traepat/<model>` addresses the underlying-model channel with multi-account failover: each account's `4031` / `4008` / `4011` codes are classified and cooled independently, exhausted channels fail fast with a `429` instead of probing every account, and cooldowns are cleared once real credits are confirmed back.
- **Quota** — free accounts have daily/weekly caps; when exhausted you get `4011` (today's usage limit reached), forwarded with a friendly Chinese message.
- **Dependencies** — pure Python standard library (including a zero-dependency AES fallback); no Node.js required.

### `trae-cli`

Installing the package also puts a `trae-cli` command on your PATH for checking/claiming check-in credits, viewing entitlements and testing chat:

```bash
uv run trae-cli status            # check-in / credit status
uv run trae-cli claim             # claim today's check-in credits
uv run trae-cli usage             # entitlements / usage (total, used %, pack list)
uv run trae-cli chat -m glm-5.2 -q "hello"
```

Auth is loaded automatically: the Work credential file `~/.buddy-proxy/trae_work.json` first (generated by `python -m buddy_proxy.auth.trae_work_login`, or via `buddy login trae`; a legacy `~/.ethan/trae_work.json` is migrated on first read), then the decrypted local Trae IDE `storage.json`. No manual token setup.

## ZCode provider (optional)

Zhipu **GLM Coding Plan** via its Anthropic-compatible endpoint, passed through directly:

```bash
uv run python -m buddy_proxy --desensitize --zcode
```

Credentials come from `ZCODE_API_KEY`, `~/.buddy-proxy/zcode_api_key` (override the directory with `BUDDY_PROXY_STATE_DIR`), or `~/.zcode/v2/config.json`. `ZCODE_OPENAI_BASE` overrides the base URL. Models are listed by `/v1/models` and appear under the `zcode/` prefix (e.g. `zcode/glm-5.3`).

There is no browser login to automate: the credential is an API key issued in the Zhipu console, and minting one is a manual click. `buddy login zcode` therefore just reports the current state and prints how to get a key — the console URL (<https://bigmodel.cn/usercenter/proj-mgmt/apikeys>), the plan purchase page (<https://bigmodel.cn/glm-coding>), and the three ways to configure it. Already have the ZCode CLI logged into coding-plan? That path works too; the key is read from its config.

When writing the key file, use `>` (overwrite) rather than `>>` (append): only the first non-empty line is read, so appending leaves the old key in effect and changing keys silently does nothing. Run `mkdir -p ~/.buddy-proxy` first if the directory does not exist yet.

The quota panel reads `/api/monitor/usage/quota/limit`. Its `limits[]` entries share one `type` (`CREDIT_LIMIT`) and distinguish windows by `unit` + `number`, not by the reset time: `unit=3`/`number=5` is the 5-hour window and `unit=6`/`number=1` is the monthly one. Name the windows from `unit`/`number` — deriving the name from "how far away is `nextResetTime`" gets both wrong, since the monthly window resets only a few days out (so it reads as "weekly") and the 5-hour window *has no* `nextResetTime` at all (so it degrades to a bare `CREDIT_LIMIT`). Rows are ordered smallest window first, so the 5-hour entry leads the card; ordering by `nextResetTime` instead drops the missing-timestamp 5-hour entry to the end and headlines the monthly bucket. Note the two windows are separate allowances to be read independently — they are not added together.

## MiMo provider (optional)

Xiaomi **MiMo** (platform.xiaomimimo.com), exposed under the `mimo/` prefix (`mimo-auto`, `mimo-pro`):

```bash
uv run python -m buddy_proxy --desensitize --mimo
```

Two auth modes, tried in order:

1. **API key** — `MIMO_API_KEY` (plus `MIMO_BASE_URL` to pick the billing / token-plan host), `~/.mimocode/auth.json` (written by MiMo Desktop's "API Key" mode), or `~/.buddy-proxy/mimo_api_key.json`.
2. **Xiaomi SSO** — sign in with `buddy login mimo` (credentials land in `~/.buddy-proxy/mimo_account.json`); if you have never logged in, the proxy falls back to reusing the login state of an installed **MiMo Desktop** app. Either way it performs the same two-stage exchange the app does to obtain a short-lived `serviceToken`; tokens are refreshed automatically and a stale one is retried once. No clipboard or cookie export needed.

```bash
uv run buddy login mimo     # opens a browser; the CLI picks up the result automatically
```

#### Sign-in (`buddy login mimo`)

Log in with a Xiaomi account in the browser — **nothing to paste, nothing to do back in
the terminal**. Same shape as `buddy login qoder` (device flow): generate a login link →
open the browser → long-poll for the result → write `~/.buddy-proxy/mimo_account.json` (`0600`).

> **Why not a local callback server** (the way `buddy login trae` works): Xiaomi's
> `callback` parameter is **server-side signed** and only whitelisted domains are accepted.
> A self-hosted `http://127.0.0.1:xxxx/cb` is rejected outright — `{"code":10025,
> "desc":"Callback连接不合法"}` — and so is Xiaomi's own `https://account.xiaomi.com/sts`
> (the signature is recomputed per request and can't be forged). So this uses the official
> `longPolling/loginUrl` endpoint instead (**no `callback`**, ticket-based).

Two details worth knowing (both found the hard way, 2026-09):

- **`loginUrl` is an API endpoint, not a page.** Opening it in a browser shows a raw
  `{"code":70016,"desc":"登录验证失败"}` blob. The real sign-in page (a SPA that renders
  the QR code client-side) is in that same response's `location` field
  (`account.xiaomi.com/fe/service/login?...`) — the CLI follows one hop to get it.
- **A browser User-Agent is required.** With a client UA the request 302s instead of
  returning the `location`-bearing body.

The ticket is valid for **300 s** (Qoder's device flow gets 10 minutes). The CLI pins its
poll deadline to that `expires_in` rather than a fixed constant — once a ticket expires,
the long-poll still hangs without returning any error, so there is nothing to react to but
your own clock.

Credential precedence: **the file written by the login flow wins**, and only if it's absent
does the proxy fall back to the desktop cookie DB — so a **fresh machine works without MiMo
Desktop installed**, and machines that do have it behave exactly as before. API keys still
outrank both (if you have one configured, logging in has no effect).

MiMo is OpenAI-shaped upstream, so requests are forwarded as-is — **except** Anthropic (`/v1/messages`) clients such as Claude Code, where the response is converted back into Anthropic events (streaming `message_start` / `content_block_delta` / `message_stop`, plus `thinking` and `tool_use` blocks) because MiMo has no native Anthropic endpoint.

The admin UI shows a quota panel with two rows: **weekly quota used** (the upstream reports *remaining* percent, which the UI converts to used) and **plan validity** (days elapsed out of the plan term). Note the two are different periods: the quota window is **7 days anchored at the subscription start**, while the plan term is usually **30 days**.

> `--mimo` is only needed when you want this channel; without it the provider is not registered and `mimo/...` model names fall through to the fallback provider.

## Qoder provider (optional)

Alibaba's **Qoder** IDE (qoder.com global / qoder.com.cn CN), exposed under the `qoder/` prefix:

```bash
uv run python -m buddy_proxy --desensitize --qoder
uv run buddy login qoder     # device flow (PKCE S256); picks the region interactively
```

The client talks to Qoder's **COSY-signed** face (`/algo/api/v2/service/pro/sse/agent_chat_generation`) — the same endpoint the official IDE uses, and the only one serving the Qwen3.8 models. Signing is reimplemented in pure Python (no extra dependencies, no vendored wasm): `Authorization: Bearer COSY.<payload>.<sig>` plus the mandatory `Cosy-User` header, with the request body in Qoder's custom-alphabet encoding. Global and CN both need signing — the region only changes *how the token is obtained*.

**Model names are lowercase real names**, not the upstream codenames:

| Model id | Upstream key | Notes |
|---|---|---|
| `qoder/qwen3.8-max` | `qmodel_38max` | reasoning + vision |
| `qoder/qwen3.8-flash` | `qfmodel` | reasoning + vision |
| `qoder/glm-5.3` / `qoder/glm-5.3-flash` | `gmodel` / `gfmodel` | |
| `qoder/kimi-k3` | `kmodel_latest` | |
| `qoder/deepseek-v4-pro` | `dmodel` | |
| `qoder/minimax-m2.7` | `mmodel` | 上游显示名即 MiniMax-M2.7 |
| `qoder/auto` / `ultimate` / `performance` / `efficient` | same | platform-routed tiers |

Older models (Qwen3.7 series, GLM-5.2, Kimi-K2.8-Preview, Cantus, Sonus, DeepSeek-Flash) are **hidden from the list but still callable** — just less clutter in `/v1/models`. All three spelling forms work: the public id, the official display name (`Qwen3.8-Flash`), and the raw upstream key (`qfmodel`); the upstream key is echoed back as `upstream_key` for troubleshooting.

Anthropic clients (`/v1/messages`, e.g. Claude Code) are supported: the proxy converts the OpenAI-shaped upstream stream into `message_start` / `content_block_delta` / `message_stop` events, including `thinking` blocks from the model's reasoning output.

Three upstream quirks are normalised per-message in `_build_upstream` before sending (all
would otherwise reject the whole request, and all are Qoder-specific — the shared converter
is left alone):

- **`developer` role is rejected at deserialisation** — rewritten to `system`, and its
  `tool_calls` (if any) dropped: `tool_calls` may only hang off an `assistant` message, so a
  `system` message carrying them poisons the `tool` reply that follows.
- **A message carrying `tool_calls` may not have `content: null`.** Anthropic's tool_use-only
  turn converts to exactly that, so any session that had *used a tool* failed while a plain
  question succeeded. The upstream's error for this is misleading — it reports
  `Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`,
  which sends you hunting in the tool-pairing code — so it is rewritten to `""` here.
  The rewrite keys off `tool_calls` rather than the role, and runs *before* the `tool_calls`
  are stripped above; a plain `content: null` assistant turn is legal and left untouched.

Upstream in-band errors carry the real cause in a `details` field (`message` alone is just
`Error in upstream response`); `_describe_upstream_error` surfaces it, and failures are
returned in Anthropic's `{"type":"error","error":{...}}` shape so Claude Code recognises them
as terminal instead of retrying ten times.

### Per-request credit cost

Qoder's upstream returns the **exact** amount charged, so nothing is estimated: every SSE
stream ends with a usage chunk carrying `credits`, `original_credits` and `billable`
alongside the token counts. The proxy reads it through and the admin UI's **Credit** column
shows it directly (2 decimals; the full-precision value stays in `logs/metrics.jsonl`).

Note the field is spelled **`credits`** (plural) here and **`credit`** (singular) on
CodeBuddy — reading only one silently drops the other channel's data, which is exactly why
Qoder requests used to log `credit: null`. `core/metrics._credit_field` accepts both, plus
`original_credits` as a fallback.

**Cached tokens are nearly free**, which is why a request with a huge input column can still
cost almost nothing. Measured on a live account:

| input | cached | uncached | output | credits |
|---:|---:|---:|---:|---:|
| 164,166 | 163,968 | 198 | 525 | 0.124 |
| 12,030 | 0 | 12,030 | 505 | 0.312 |

So ~136k-input requests billed at 0.07–0.13 are expected, not a bug: roughly 99% of the
input hit cache.

### Daily activity credits (check-in)

Qoder runs a **daily "claim 100 Credits" campaign** (the popup the desktop app opens on
launch). It is served from a different face than chat — `{openapi}/sash/api/v1/me/campaigns` —
and, unlike `/algo/**`, it **needs no COSY signature**: a plain `Authorization: Bearer <dt-token>`
works (the desktop app calls it from the Electron main process, logged as `requestSource: "native_main"`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/sash/api/v1/me/campaigns` | list campaigns + claim status |
| `GET` | `/sash/api/v1/me/campaigns/{id}/reward` | grant result |
| `POST` | `/sash/api/v1/me/campaigns/{id}/claim` | **claim** |

The Qoder channel declares `supports_checkin`, so it shows up in the admin UI's
**打卡 & 额度** panel next to CodeBuddy and is included in auto check-in.

Three behaviours worth knowing (all verified against a live account, 2026-09):

- **It is per-day, keyed by a new campaign id.** The window is literally `10:00 → next 09:59`
  (UTC+8), and `campaignKey` increments daily (`act-20260923-556` → `-557`), with `campaignId`
  a fresh UUID each day. Always re-list; never cache the id across a day boundary.
- **Claiming is idempotent.** Re-claiming an already-claimed campaign returns
  `200 {"status":"CLAIMED","replayed":true}` — that is a *replay*, not a new grant
  (`claimedAt` is the past one). Only `replayed: false` means credits were actually granted.
- **Don't trust the top-level `claimable` flag** to decide whether to claim: it also covers
  `VIEW_DETAILS` campaigns and unopened windows. The real test is per entry:
  `actionType == "CLAIM_BENEFIT" && claimStatus == "CLAIMABLE"`.

Activity grants like these land in `/api/v2/quota/usage` as **dedicated resource packages**
(`dedicatedResourcePackages`), which coexist with the subscription quota and add-on packs —
they are part of the account's total allowance, each with its own (earlier) expiry. The admin
UI's quota panel shows every available package as its own row, so the total matches what the
account really has; packages with `available: false` (expired/invalidated) are skipped.

> `--qoder` is only needed when you want this channel; without it the provider is not registered and `qoder/...` model names fall through to the fallback provider.

## Connect clients

### Codex CLI

`~/.codex/config.toml`:

```toml
[model_providers.codebuddy]
name = "CodeBuddy (via local proxy)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"

[profiles.codebuddy]
model = "glm-5.3"
model_provider = "codebuddy"
```

```bash
codex --profile codebuddy "your task"
```

### Claude Code / CC Switch

```json
{
  "DeepSeek-V4": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

### OpenCode

`opencode.json`:

```json
{
  "model": "codebuddy/glm-5.3",
  "providers": {
    "codebuddy": {
      "name": "CodeBuddy (via local proxy)",
      "package": "@opencode-ai/ai/providers/openai-compatible",
      "settings": { "baseURL": "http://127.0.0.1:8787/v1", "apiKey": "noop" },
      "models": {
        "glm-5.3":         { "modelID": "glm-5.3",         "name": "GLM-5.3" },
        "deepseek-v4-pro": { "modelID": "deepseek-v4-pro", "name": "DeepSeek V4 Pro" },
        "kimi-k2.7":       { "modelID": "kimi-k2.7",       "name": "Kimi K2.7" }
      }
    }
  }
}
```

### Grok CLI

`~/.grok/config.toml`:

```toml
[models]
default = "hy3"

[model.hy3]
model = "hy3"
base_url = "http://127.0.0.1:8787/v1"
name = "HY3 Main"
api_key = "noop"

[model.dv4f]
model = "deepseek-v4-flash"
base_url = "http://127.0.0.1:8787/v1"
name = "DeepSeek V4 Flash"
api_key = "noop"
```

### Oh My Pi (OMP)

`~/.omp/agent/models.yml`:

```yaml
providers:
  codebuddy:
    baseUrl: http://127.0.0.1:8787/v1
    api: openai-completions
    auth: none
    models:
      - id: hy3
        name: Hy3 (CodeBuddy)
        reasoning: true
        contextWindow: 192000
        maxTokens: 64000
      - id: deepseek-v4-flash
        name: DeepSeek V4 Flash (CodeBuddy)
        reasoning: true
        contextWindow: 1000000
        maxTokens: 50000
```

### Other OpenAI-compatible clients

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: blank (or your `--api-key`)
- Model: any id from `/v1/models`, e.g. `glm-5.3`, `deepseek-v4-pro`, `kimi-k2.7`, `auto`

---

## Command-line options

```
--host HOST               bind address (default 127.0.0.1; proxy.sh uses 0.0.0.0)
--port PORT               bind port (default 8787)
--endpoint ENDPOINT       CodeBuddy backend address
--session-file PATH       session file (default ~/.codebuddy-session.json)
--log-file PATH           JSONL log (default <project>/logs/buddy-proxy.jsonl, override via BUDDY_PROXY_LOG_FILE)
--desensitize             enable desensitization (recommended)
--optimize-context        enable message compression (recommended for Codex)
--default-model MODEL     default model, e.g. zcode/glm-5.3; used when a request has no `model`
                          field. Seeded into the settings file on first start; afterwards
                          ~/.buddy-proxy/settings.json (editable from the admin UI) wins
--default-provider NAME   fallback channel for model names that match no provider
                          (codebuddy/zcode/trae/doubao/mimo, default codebuddy)
--trae                    enable the Trae provider (decrypts the Trae IDE login)
--zcode                   enable the ZCode provider (Zhipu GLM, Anthropic passthrough)
--doubao                  enable the Doubao provider (drives the desktop app over CDP)
--mimo                    enable the MiMo provider (API key or MiMo Desktop login state)
--login                   browser login at startup (opens the browser; prints the login URL)
--no-browser              don't auto-open a browser. Implicit/background re-auth (e.g. the
                          auto-checkin poll) never opens a browser and never prints a login
                          URL regardless; it only logs one `[Auth] ...` line. Use --login
                          when you actually want the interactive login link
--verbose-llm             emit expanded safe diagnostics (never logs request/response bodies, tokens, or UIDs)
--mock-dir DIR            serve recorded fixtures (testing)
```

Env vars: `BUDDY_PROXY_HOST`, `BUDDY_PROXY_PORT`, `CODEBUDDY_ENDPOINT`, `CODEBUDDY_MODEL`, `BUDDY_PROXY_LOG_FILE`, `BUDDY_PROXY_SETTINGS` (settings file path), `BUDDY_PROXY_STATE_DIR`, `BUDDY_PROXY_ADMIN_OPEN=1` (lift the localhost-only restriction on admin endpoints), `PROXY_DEFAULT_PROVIDER` (fallback channel, default `codebuddy`), `TRAE_ENABLED` / `ZCODE_ENABLED` / `DOUBAO_ENABLED` / `MIMO_ENABLED` (`1` enables that provider, same as the flags), `TRAE_TOKEN` / `TRAE_USER_ID` (skip Trae IDE decryption and use these directly), `ZCODE_API_KEY`, `ZCODE_OPENAI_BASE`, `MIMO_API_KEY` / `MIMO_BASE_URL`, `BUDDY_CLIENT_NAMES_FILE` (override the client-name map used in the request log).

Trae stream tuning: `WB_TRAE_HEARTBEAT_INTERVAL` (heartbeat while waiting for the buffered upstream response, seconds, default 45, `0` disables — keeps clients with per-chunk timeouts like Ethan's 120s from aborting long generations), `WB_TRAE_NATIVE_TOOLS` (native channel for all Trae requests — native function calling for tool requests, preset-free chat for plain ones; default `1`; `0` falls back to the legacy prompt-taught text protocol with leak guards), `WB_TRAE_IDE_VERSION_CODE` (Trae client version header, default `20260906` — the upstream gates per-model capabilities by this header; raise it when a model suddenly 4001s), `WB_TRAE_NONSTREAM_MAX_S` (non-streaming aggregation cap), `WB_TRAE_SEMANTIC_TIMEOUT`, `WB_TRAE_TOKEN_KEEPALIVE_S` (background token refresh interval).

## API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/ui`                 | admin UI (`/` 302-redirects here) |
| GET  | `/ui/api/*`           | admin API (overview / models / stats / benefits / settings / checkin / test / logs / model-toggle / model-schedule / traepat accounts & model-status, localhost only) |
| GET  | `/health`             | service + auth status |
| GET  | `/v1/models`          | model list |
| POST | `/v1/chat/completions` | OpenAI chat (tools + streaming) |
| POST | `/v1/responses`       | Responses API (Codex CLI) |
| POST | `/v1/messages`        | Anthropic Messages (Claude Code) |

Endpoints authenticate using the local session; no extra token is required.

## Disclaimer

For learning and research purposes only. Please comply with CodeBuddy's Terms of Service. Use at your own risk.
