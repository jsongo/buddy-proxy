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
                         #   credit_estimate, desensitize
  protocols/             # anthropic_adapter, responses_adapter,
                         #   responses_projection, dsml_parser
  web/                   # routes, ui, model_list + models_config.json + static/
  providers/             # base.py (BaseProvider), zcode.py
  auth/                  # login, trae_work_login, trae_work_login_server
  codebuddy_provider/    # default provider pkg; client.py = CodeBuddy HTTP client
  trae/                  # trae domain (credentials, transport, sse, pat, ...)
  doubao/                # doubao domain (provider.py, cdp_client)
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
- **`web/ui.py`** provides localhost-only administration endpoints and the
  bundled UI. It may update runtime settings but must use `core/settings.py`
  for persistence.

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
