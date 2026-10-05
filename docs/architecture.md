# Architecture

`buddy-proxy` is a local FastAPI gateway that exposes OpenAI Chat Completions,
Responses, and Anthropic Messages APIs over several upstream providers.

## Package layout

The flat package root was reorganized into domain subpackages (no
backward-compat shims — imports point at the new paths):

```text
src/buddy_proxy/
  __main__.py            # CLI entry + repo-root resolution (parents[2])
  trae_provider.py       # published trae-cli entrypoint + trae façade
  core/                  # state, settings, paths, logging_setup, metrics,
                         #   credit_estimate, desensitize, checkin, errors
  protocols/             # anthropic_adapter, responses_adapter,
                         #   responses_projection, dsml_parser (解析层),
                         #   dsml_scanner (底层扫描原语)
  web/                   # routes, ui, model_list + models_config.json + static/
  providers/             # base.py (BaseProvider), zcode.py
  auth/                  # login, trae_work_login, trae_work_login_server
  codebuddy_provider/    # default provider pkg; client.py = CodeBuddy HTTP client
  trae/                  # trae domain (credentials, transport, sse, pat, ...)
  qoder/                 # qoder domain (provider + convert/errors/quota helpers)
  doubao/                # doubao domain (provider, cdp_client, payloads, ws)
  dumate/                # DuMate domain (discovery = local-proxy discovery + inapp-key,
                         #   cookies = pure-stdlib AES-256-GCM bceConsole cookie decrypt,
                         #   checkin = daily sign-in, provider)
```

`core.state` imports `CodeBuddyClient` / `BaseProvider` only under
`TYPE_CHECKING` — importing them at runtime would recreate a
`codebuddy_provider` package → `observability` → `core.state` import cycle.

## Request path

```text
/v1/chat/completions ─┐
/v1/responses         ├─ web/routes.py: protocol normalization
/v1/messages          ┘
                         │
                         ▼
             codebuddy_provider.forward.forward_chat()
             - defaults, explicit provider prefix, model lookup
             - disabled/scheduled model policy
             - unified metrics instrumentation
                         │
             ┌───────────┼────────────┐
             ▼           ▼            ▼
        CodeBuddy      Trae/PAT      ZCode / Doubao
```

- **`web/routes.py`** owns public protocol endpoints and request parsing.
- **`codebuddy_provider/forward.py`** owns cross-provider routing and model
  availability policy. New protocol endpoints must use this path rather than
  call a provider directly.
- **`providers/base.py`** defines the deliberately small `BaseProvider` contract:
  model catalog, authentication, forwarding, health, and optional check-in /
  quota capabilities.
- **`codebuddy_provider/`** contains the default provider's routing,
  instrumentation, upstream pipeline, provider implementation, and the
  `client.py` CodeBuddy HTTP client.
- **`trae/`** is split by responsibility (credentials, transport, SSE,
  native tools, text fallback, and PAT). `trae_provider.py` remains the
  published `trae-cli` entrypoint and trae façade (not a shim to remove).
- **`web/ui/`** provides localhost-only administration endpoints (split by
  responsibility: channels, models, settings, queries, page) and the bundled
  UI under `web/static/` (an `index.html` skeleton plus `style.css` and six
  JS files, served from `/ui/{name}`). `benefits.js` (calendar + quota fold +
  channel-specific panels) was split by responsibility once it passed 700
  lines: `benefits_accounts.js` holds the shared multi-account card helpers
  (delete-confirm modal / move / delete / snapshot backfill) that new
  multi-account channels reuse, `benefits_checkin.js` the checkin rows + Kimi
  panel, `benefits_panels.js` the Trae PAT / Antigravity / Qoder panels.
  It may update runtime settings but must use `core/settings.py` for
  persistence.

## Runtime composition

`__main__.py` parses CLI/environment configuration, instantiates enabled
providers, loads persisted settings, and creates `ProxyState`. The application
currently uses a module-level FastAPI app and `ProxyState`; this is intentional
for the single-process local deployment model. Do not introduce an app factory
as a drive-by refactor: it affects lifecycle code, compatibility shims, and
existing isolated tests. Revisit it only when supporting multiple app instances,
workers, or explicit provider shutdown lifecycles.

## Models and settings

- `web/models_config.json` is the source for static CodeBuddy/PAT catalog
  metadata. Dynamic providers expose their catalog through
  `BaseProvider.models()`.
- Any persisted model identity must be built with `core.settings.model_key(
  provider, model)`. It normalizes the legacy `workbuddy` name to `codebuddy`;
  bypassing it can make UI policy and runtime routing disagree.
- `core/settings.py` owns local settings path, normalization, and atomic
  persistence. Callers must not open or write the settings JSON directly.
  Every overwrite keeps the previous version as `settings.json.bak` first, so a
  UI write is always reversible. Backing up is best-effort and must never make
  a save fail.
- `model_order` (settings key) maps a **bare model name** to an ordered list of
  `provider/model` candidates, tried top-down by `forward._forward_with_order`.
  The key is the name the client asks for, not a channel-scoped pair: one entry
  then covers every channel that publishes it (a `glm-5.3` on four channels is
  one config, not four), and the runtime picks whichever channel claims the name
  first. `forward` also accepts the historical `<provider>/<model>` key form so
  configs written by older versions keep firing.
  **Failover is only legal when an attempt failed before any byte was committed
  to the client** — a retryable `HTTPException`, a transport-level exception
  (`_RETRYABLE_EXC`: `httpx.HTTPError` / `OSError`), or a non-2xx
  `JSONResponse`. A `StreamingResponse` is never replayed (double-billing risk;
  same invariant as `trae/pat/chat.py`). Known blind spot: codebuddy reports
  upstream stream errors as an *in-band* error chunk, so its stream failures
  never fail over. `metrics.SSEErrorExtractor` now spots that chunk in
  `observability._metrics_stream` and records the request as failed, so the
  metrics no longer show a false green — but the *routing* behaviour is
  unchanged (a stream that already started is still never replayed).
  Conversely, **programming errors are not failover material**: a `TypeError`
  escaping a provider is re-raised, not treated as an upstream outage, so a real
  bug surfaces as a 500 instead of being masked by a working fallback channel.
- Routing precedence: an explicit `provider/model` prefix in the request is a
  deliberate instruction and returns before `model_order` is consulted, so a
  prefixed request never fails over. Failover applies to bare model names,
  where the router resolves the owning provider itself.
- A prefix naming a channel in `settings.KNOWN_PROVIDER_IDS` that is *not*
  registered this run is a **400 `provider_disabled`**, not a fallthrough.
  Letting it fall through sent the whole `qoder/...` string to the fallback
  channel, which the upstream rejected as `model [...] service info not found`
  — the client then reported "model not found", hiding the real cause (the
  channel was never enabled). Prefixes that are *not* channel names
  (`openrouter/...`) still fall through: `provider/model` is a legal plain
  model id, and `trae`/`traepat` exceptions aside, the router must not claim it.
  The error text comes from `settings.PROVIDER_ENABLE_HINTS` rather than being
  assembled from the prefix — `traepat` has no `--traepat` flag (it rides the
  `--trae` branch, gated by `TRAE_PAT_BEARER`), so a generated hint would name
  a flag that does not exist.
- `core/cooldown.py` owns transient target health: a failed target is skipped
  for a short TTL (escalating on repeat failure). It is deliberately **in-memory
  and not persisted** — a restart is a legitimate reason to re-probe, and user
  intent lives in `settings.json`. Not every failure is cooldown material: a
  **local DNS resolution failure is machine-level**, not this upstream's fault.
  Marking it cools *every* candidate at once and amplifies one blip into a
  whole-model outage with no re-probe for five minutes (measured 2026-10-02:
  all three candidates marked, then `model_order_skip reason=cooldown` for
  5 minutes straight, zero upstream requests dispatched). `core/errors.py`
  (`is_local_dns_failure`) classifies it and `forward` skips the mark — the
  failover still moves to the next candidate, it just leaves no trace behind.
  Classification **must walk the `__cause__` chain**: every provider converts
  the `httpx`/`httpcore` error into `HTTPException(502)` at its boundary, so
  checking the top-level type alone misses all of them. `core/errors.py` is a
  leaf module (stdlib only), same convention as `core/cooldown.py`.
  `describe_exception` lives there too: `httpcore` maps some failures onto
  exceptions whose own `str()` is empty (`httpx.ReadError()`), and a bare `%s`
  on those logs a blank line — it falls back to the type name and the first
  non-empty cause.
  DNS is not the only machine-level failure mode. Same evening, ~1 hour later:
  the local Clash tunnel died and qoder/codebuddy/trae all failed within 50
  seconds — but with genuine `502 Bad Gateway` responses (the tunnel's own
  error page), which is *not* classifiable per-request: the tunnel error body
  and a real upstream 502 look identical once wrapped. So `core/cooldown.py`
  uses a statistical signal instead: **≥2 distinct channels failing for the
  same model within a 60s window ⇒ machine-level burst** (network-class
  failures only — transport errors and 5xx; 429 quota rejections are the
  upstream's own state and never enter the window, so simultaneous rate
  limiting keeps its full 5-minute cooldown and escalation) — the current target
  and the already-marked ones in the window are (re)marked for only 30s and
  never count toward escalation. The first-failing target gets shortened
  retroactively (it was marked 5 min before the second channel's failure
  proved the burst); burst evidence is wiped by `clear()` so a manual
  clear-cooldown can't be overridden by stale window entries. Accepted
  trade-off: two genuinely-broken channels failing within 60s also get 30s
  marks — failover still works, the marks just rotate faster.
- The `model_order` editor lives in its own **Model order** tab of the bundled
  `/ui` page: one card per configured model, expanding to the reorderable target
  list. **Its render source is `GET /ui/api/model-order`, which returns the
  settings field verbatim** — one item per key, so the page shows exactly what
  the config holds. It deliberately does *not* drive cards off the channel
  catalog: `GET /ui/api/models` annotates each (channel, model) pair, so a name
  published by four channels came back four times and the page drew four
  identical cards. Its channel list and model choices (for the "add model"
  picker) come from `GET /ui/api/model-order/options` (the page's global
  `MODELS` is only populated once the models tab has loaded, so the editor must
  not depend on it). Targets are picked from that list but remain free-text,
  because an upstream may accept an id its catalog does not advertise.
- A model card's key is the **bare model name** — no owning channel. The card is
  the config key, and the runtime bare key fires on every channel that publishes
  it, so a channel-scoped key would silently narrow the rule to one channel.
  Related mismatch worth remembering: the catalog's `m["id"]` carries a channel
  prefix (`qoder/deepseek-v4.1-flash`) while `model_order` targets and metrics
  are recorded against the bare name. Looking up the per-model request counts on
  the models tab with the prefixed id made that column read ~0 for every channel
  that prefixes its ids.
- Editing is **page-only**. There is deliberately no row-level order button on
  the models tab: that table's action column grew crowded and the entry point
  became unfindable. The row keeps a `⇄ n` badge (targets, plus `⏸ n` cooled) and
  a clear-cooldown button, nothing more.

## Check-in rotation

Check-in ("打卡") is per-provider: a provider opts in with
`supports_checkin = True` and implements `checkin_status` / `checkin_claim` /
`quota`. `benefits.BenefitsManager` aggregates them for the admin UI and runs
the auto-claim loop. Two invariants matter because getting them wrong is
silent:

- **A provider's rotation period is not necessarily a calendar day.** Qoder's
  campaign window is `10:00 → 09:59` next day (verified 2026-09), while
  CodeBuddy / Trae rotate at local midnight. Local history
  (`logs/checkin.jsonl`) dedupes by *calendar day*, so an early-morning poll
  can record the previous Qoder round as "today's claim" and make the rest of
  the day look done. **Anything the upstream reports as claimable therefore
  wins over the local record** (`benefits.claimable_now`) — both for the UI's
  `done_today` and for the auto-claim loop. A provider that contradicts itself
  (`checked_in` *and* `claimable`) is treated as already claimed, so a
  malformed reply costs at most a skipped round rather than a claim request
  per poll.
- **The timestamp source must be reported, not assumed.** `core/checkin.py`
  computes `next_ts` (the moment the current state flips) plus
  `next_ts_source`. Only Qoder's upstream actually returns a window
  (`startAt`/`endAt`, so `upstream`); CodeBuddy returns only the whole campaign
  season and Trae no time fields at all, so midnight is *inferred* from the
  real claim timestamps and must be labelled `inferred` — the UI shows "≈".
  When `next_ts` cannot be computed (season over, campaign inactive), the field
  is omitted rather than filled with a stale timestamp.
- **A cached status snapshot expires at its own rotation, not just its TTL.**
  The UI counts down to `next_ts`, so the refresh right after that moment must
  actually show the new state — but the 300 s snapshot cache would otherwise
  serve the pre-rotation state, leaving "已签到" and a disabled button for up to
  five more minutes (long enough for Qoder to lose a whole round, since its
  window expires on miss). `benefits._state_flipped` therefore treats a *past*
  `next_ts` as an earlier expiry, which is free: the cached entry already
  carries the moment it stops being true. Bounded by `FLIP_GRACE_S` (1 h) so an
  upstream that keeps returning a long-past timestamp cannot render the cache
  permanently useless and hammer the upstream on every poll.
- **Expiry and reset are different things and live in different fields.** A
  quota item carries `expire_ts` (the allowance is voided — plans, packs,
  check-in credits) and `reset_ts` (it refills on a cycle — ZCode's 5-hour
  window, antigravity's weekly pool). Upstreams rarely label which one they
  mean, and the *same field name* means different things in different
  channels: Qoder's `expiresAt` is an expiry, MiMo's `nextResetTime` a reset,
  Trae's `entitlement_base_info.end_time` an expiry — but Trae **PAT's**
  `end_time` is a reset (weekly/daily pools refill at midnight). All of them
  were once fed into `reset_ts` and rendered as "· 重置", which is how Qoder's
  "expires 10-30" displayed as "10-30 resets". Guessing wrong is not
  cosmetic: `benefits._expiring` only
  reads `expire_ts`, so a reset misfiled as an expiry would put "your 5-hour
  window expires in 2 hours" on the warning banner every single round. Items
  also carry `unit` (`credit` / `day` / `count` / `permille`), which gates the
  banner's amount filter — a 200-credit check-in pack expiring in 7 days is
  noise, a 4000-credit plan is not; days and counts are not comparable to a
  credit threshold and are filtered by time alone.
- **A quota reading's sign convention is part of its meaning.** Antigravity
  reports *remaining* (`remainingFraction=1` means full) and has no interface
  that exposes the weekly pool at all; CLIProxyAPI shows `100%` for the same
  accounts, which is also *remaining*. buddy renders both `remaining`/`total`
  and `percent`/`used` (progress-bar semantics), so a full allowance shows as
  "剩 1000 / 1000 · 已用 0%" — correct, but it read as "quota is 0" until the
  item also spelled out "满额" and listed `models_in_group`. When one upstream
  hands the same single value to two UIs, a discrepancy is a sign convention,
  not a data difference; say so in the item's `note` rather than guessing.
- **Providers report every quota item; the frontend decides what to show.**
  `quota()` must not truncate `items` for display. CodeBuddy used to return
  only the first 4 packs (`if len(packs) < 4`) and the cut was invisible in a
  specific way: the dropped rows are never sent, so nothing downstream can
  know they exist or say so. Trae has the same shape of bug historically
  (deduped by name, capped at 3, hiding 12 unspent allowances). Row count is a
  *view* concern, so it lives in the view: `benefits.js` renders items through
  `quotaItemsHtml`, which shows only items with remaining allowance, folds the
  used-up ones (remaining ≤ 0) into a collapsed block **after** the live ones,
  and clips the whole region past `--qfold-max` with an expand button. Three
  rules are load-bearing. (1) *Items with unknown remaining are never folded* —
  a PAT failure notice or `reset_pending` carries text, not a number, and
  reading that as zero would hide the one row that matters. (2) *The fold
  decision uses measured height, not an item count* — in the two-column layout
  a narrow card's usable height differs from a full-width card's. (3) *Expand
  state lives in `QUOTA_FOLD`, not the DOM* — the panel is rebuilt from
  `innerHTML` every 30 s (same reason as `_state_flipped` above: the page's own
  refresh cycle is the thing that would otherwise undo the user's action).
  Anything the backend withholds for display reasons is unreachable by
  construction, so "keep the payload small" is not a valid reason to trim here.

## Logging and privacy

**Never log token, UID, request body, response body, tool arguments, or bearer
credentials.** Diagnostic code records only safe metadata such as provider,
model, counts, byte length, status, and short one-way hashes. `--verbose-llm`
and `WB_DEBUG_DUMP` retain compatibility but may add only safe summaries.

## Validation before changing behavior

```bash
uv run ruff check src tests
uv run pytest -q
```

Keep provider changes small and add an offline regression test. Live upstream
probes are billable and must never print credentials or request bodies.
