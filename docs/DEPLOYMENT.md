# Deployment assessment

Status: assessment only. No platform or runtime/storage change has been approved and none is
implemented. The current deployment model is a single local process with a local SQLite file.
This document evaluates that model operationally and compares it with serverless candidates,
based on accepted official-source research. It does not approve a platform.

Scope notes:

- Companion state is portable in principle: identity, memory, and affect are all stored and
  served through a provider-neutral context API (`POST /state/v1/context`) plus state endpoints.
- The implementation today is SQLite (FTS5 trigram, the sqlite-vec native extension), local
  files for config/identity/prompts, and in-process background loops. No port has been made.
- API contracts live in `docs/API.md` and the configuration reference lives in
  `docs/CONFIGURATION.md`. This document links to them when relevant.
- No persona examples are included here by design.

## 1. Current deployment model (verified locally)

Verified means exercised by the local test suite and installed-wheel checks on a normal CPython
3.11+ host. It does not mean exercised on any hosted runtime.

| Component | Current implementation | Where it lives |
| --- | --- | --- |
| Storage | One SQLite database file `state.sqlite3` under `data_dir`; WAL mode, busy timeout, foreign keys; schema version 3 | `src/companion_gateway/database.py` |
| Full-text search | SQLite FTS5 virtual table, trigram tokenizer, content-synced by triggers | `database.py` |
| Vector search | sqlite-vec extension (`sqlite-vec==0.1.9`) with `vec_distance_cosine` over stored float32 blobs; derived, rebuildable index | `semantic.py` |
| HTTP API | FastAPI + uvicorn on `host:port` (default `127.0.0.1:8765`); state endpoints optionally gated by `X-Companion-Token` | `api.py`, `cli.py` |
| Scheduling | In-process asyncio background tasks for proactive evaluation and embedding backfill; proactive leases, dedupe keys, and retry delays are persisted in SQLite | `api.py`, `proactive.py`, `semantic.py` |
| Identity and prompts | Raw identity text file and packaged/overridable prompts JSON, resolved from config-relative paths at process startup; loaded once per process | `identity.py`, `prompts.py`, `service.py` |
| Secrets | Config stores environment variable names, not values; provider keys are read from the environment only for upstream proxy and embedding calls | `config.py`, `api.py`, `semantic.py` |
| Backup | `companion-gateway backup <path>` uses the SQLite online backup API, safe while the service writes | `database.py`, `cli.py` |
| Service unit | `deploy/companion-gateway.service` is a `systemd --user` unit example | `deploy/` |

Testing evidence is local: the project's pytest suite passes, Ruff is clean, and installed-wheel
checks pass in a scratch environment. Those tests demonstrate packaging and behavior on a local
interpreter; they do not test any hosted deployment.

## 2. Operational guidance for the current local model

These are the supported operating conditions. Treat them as the baseline that any proposed
platform must match or explicitly trade off.

### 2.1 Single instance on a durable volume

- Run one gateway process against one `state.sqlite3` file. SQLite WAL supports a single
  writer; the in-process schedulers assume one evaluator. Do not run two processes on the same
  database file.
- The volume must persist `state.sqlite3` and its `-wal`/`-shm` companion files across
  restarts. Any filesystem that resets content on restart (container scratch, ephemeral
  instance disk without a mounted volume) loses all memory, affect, evergreen facts, and
  proactive events.
- One deployment is one companion identity. This model does not promise multi-tenant
  isolation, per-user authentication, or horizontal scaling. A second companion needs a second
  deployment with its own config, data directory, port, and service unit.

### 2.2 Network exposure, TLS, and companion token

- The default bind is `127.0.0.1`. For access from other devices, terminate TLS at a reverse
  proxy in front of the gateway or keep the gateway reachable only over a private network.
  Do not expose the plaintext HTTP port publicly.
- Set `api_token_env` to the name of an environment variable holding a long random token.
  Enforcement only happens when that environment variable is set to a nonempty value at
  runtime; naming a field in config alone does nothing. When it is set, every `/state/v1/*`
  endpoint requires an `X-Companion-Token` header that exactly matches the value, and requests
  without it are rejected with 401. When `api_token_env` is empty, or the named variable is
  unset or empty, every `/state/v1/*` endpoint is unauthenticated; expose the service only on
  a trusted local or private network in that case.
- `GET /health`, `GET /v1/models`, and `POST /v1/chat/completions` are not companion-token-gated.
  The proxy can use a server-owned provider key from `upstream.api_key_env`: when that variable
  is set to a nonempty value the proxy sends it as the bearer token and does not forward the
  caller's `Authorization`; otherwise it forwards the caller's header. Because the proxy and
  health paths are not companion-token-gated, the entire service must sit behind edge access
  controls (reverse proxy, private network, TLS) when exposed beyond localhost.
- The context API and all state endpoints work with no provider credentials at all. Provider
  secrets are used only for the upstream chat proxy and the optional embedding client.

### 2.3 Provider secrets

- `upstream.api_key_env` (default `UPSTREAM_API_KEY`) and `memory.embedding.api_key_env`
  (default `EMBEDDING_API_KEY`) name environment variables. The config migration tool refuses
  to emit literal credentials into config files.
- Export the key variables in the service environment, keep them out of config files and logs,
  and treat log content as possibly containing prompt and conversation text.

### 2.4 Identity and prompt files are deploy-time immutable

- The identity text file and any prompts/emotions overrides are resolved from config-relative
  paths once at process startup. A change requires a deliberate reload or redeploy of the
  service. There is no hot reload, no admin endpoint that rewrites these files, and no runtime
  mutation of instruction text.
- Version these files with the deployment. A rollback restores the previous file versions
  alongside the previous database backup.

### 2.5 Backups and restore

- Back up all user-authored state, not only the database: the config file (JSON preferred),
  the identity text file, custom prompts/emotions files if any, and the database.
- Create database backups with `companion-gateway backup <path>` while the service runs.
  Do not copy `state.sqlite3` while writes are active.
- Restore by stopping the service, placing a schema-version 2 or 3 database file back into
  `data_dir`, and starting again. Older schema versions are rejected. Keep a backup before any
  in-place schema upgrade.

### 2.6 Memory privacy

- Default `memory.retrieval_mode` is `lexical`: search and context building stay entirely
  local, no text leaves the host for retrieval.
- `hybrid` mode sends message text chunks to the configured embedding endpoint
  (`memory.embedding.base_url`) to produce vectors that are stored locally. That outbound call
  is optional and only happens when an embedding endpoint and model are configured.
- The upstream chat proxy sends the conversation to the configured provider by definition.
- Affect computation is local and deterministic; it does not call a model. Nothing in this
  document assumes the future decision-module design described in
  `docs/DECISION_MODULE_DESIGN.md`.

## 3. Serverless candidates and accepted research

The following is a summary of accepted official-source research. Unknowns are stated as
unknowns; no new research was performed for this document.

Cloudflare Workers (Python):

- FastAPI/ASGI is supported on the Python Workers runtime (Pyodide).
- The Python Workers filesystem is ephemeral and threading/multiprocessing are nonfunctional.
  `sqlite-vec` is a native extension and is not a confirmed supported package; do not assume a
  port works until a runtime test proves it.
- httpx normal socket behavior is unverified on this runtime; the official FFI fetch route is
  the documented networking path and needs a runtime smoke test.
- In-process background loops cannot survive an isolate lifetime. `waitUntil` is not a durable
  scheduler.

Cloudflare Containers:

- Container disk is ephemeral across sleep/restart; a restart runs a fresh image and local
  SQLite is lost. Containerizing alone does not make state durable.
- Containers require a paid plan (Workers Paid applies); general availability was not verified
  for every claimed feature at research time.
- Persistent snapshots were planned at research time; do not promise availability.
- Cloudflare storage services (D1, Durable Objects, Vectorize) are possibilities but are not
  mandated. D1 parity for FTS5, vector search, custom SQL, and transaction semantics must be
  checked and tested before any migration. Alarms/Cron can provide durable scheduling only
  with idempotent event handling.

Generic Linux serverless containers:

- A generic container platform preserves the existing Python dependencies, but disk durability
  and scale-to-zero behavior are provider-specific and must be verified for a chosen provider.
  No provider has been chosen.

A persistent-volume, single-instance host keeps code churn lowest and is the baseline above.
It is not claimed to be "fully serverless".

No external runtime deployment has been tested. Live deployment remains unverified until a
chosen platform is explicitly exercised.

## 4. Current versus proposed comparison

| Concern | Current (verified) | Proposed serverless (unverified) |
| --- | --- | --- |
| Data durability | SQLite file on a persistent volume; WAL; online backup CLI | Ephemeral container/worker disk loses data on sleep/restart; a durable platform store is required |
| Full-text search | FTS5 trigram, trigger-synced | Needs an equivalent in the target store; D1 FTS parity must be tested |
| Vector search | sqlite-vec native extension over local blobs | sqlite-vec unsupported/unverified on Python Workers; Vectorize is a different API and needs an adapter |
| Scheduling | In-process asyncio loops with DB-backed leases, dedupe, retry | Loops cannot outlive an isolate; needs alarms/Cron/Durable Objects with idempotent handlers |
| Identity/prompts | Config-relative files, loaded once per process | Deploy-time files; reload semantics are platform-specific and must be defined |
| Auth | Optional `X-Companion-Token` on state endpoints | Must reimplement token enforcement and TLS termination at the platform edge |
| Backups | Online backup CLI with restore guidance | Depends on platform snapshot/export story, not yet promised |
| Code churn | None (current model) | Storage, HTTP, scheduling, and migration work required (section 5) |

## 5. Exact port work required (not approved, not implemented)

No piece of this list is done. Changing runtime or storage requires explicit approval (section 6).

Storage:

- Replace the SQLite connection layer (`database.py`) with a target-store adapter and verify:
  FTS-equivalent search and ranking, vector distance semantics equivalent to
  `vec_distance_cosine`, `BEGIN IMMEDIATE`-style transaction and unique/dedupe constraint
  behavior, trigger-maintained index consistency, schema versioning and in-place migration,
  online backup, and `PRAGMA integrity_check` equivalents.
- Port the semantic chunker fingerprint, embedding key, and blob vector storage to the target
  store. The disposable derived index must stay rebuildable from canonical messages.

HTTP/transport:

- Verify httpx behavior on the target runtime or replace it with the platform-native fetch/FFI
  path. The proxy path includes streaming (`aiter_bytes` chunk parsing), final assistant
  ingestion after stream completion, the selected `Authorization` value (the configured
  upstream key or the caller's header) plus content-type, and `timeout_seconds` handling.
  The routing headers (`x-conversation-id`, `x-harness`, `x-companion-route`) are consumed by
  the gateway for state and ingestion; they are not forwarded upstream.
- Reimplement token enforcement for state endpoints, plus TLS termination and any request
  body/header limits imposed by the platform.

Scheduling:

- Replace the in-process asyncio loops with a platform scheduler (Cron triggers, alarms, or
  Durable Objects) and design idempotency around the existing dedupe keys, leases, and
  acknowledge/retry semantics. A restarted or cold worker must not double-evaluate or double
  send proactive events.
- Turn embedding backfill into a scheduled job with the same batch and cooldown behavior.

Migration:

- Provide an export/import path from the current SQLite schema to the target store, versioned
  with rollback, and restore from existing backups verified against a fresh deployment.

No immediate install-all-Cloudflare-services action is proposed. Each candidate store must
pass parity tests (section 6) before adoption.

## 6. Decision checklist

Before choosing a platform or storage runtime, confirm each item:

1. Is single-instance persistent-volume hosting acceptable? If yes, it is the lowest-churn
   path and does not require the serverless work above.
2. If a serverless platform is chosen: which provider, and which storage and scheduler
   services does it use?
3. Storage parity: FTS, vector distance, transactions, dedupe, and integrity checks are
   tested against the current database before any migration.
4. Scheduler idempotency: duplicated evaluation and delivery are explicitly prevented.
5. Auth and TLS: all state endpoints are protected at the edge; secrets stay in environment
   variables.
6. Backup, restore, and rollback: verified against a fresh deployment, not just on the local
   host.
7. Prompt/identity immutability: instruction text changes only via deliberate redeploy.
8. Approval gate: changing runtime or storage is approved explicitly before any port starts.

## 7. Future deployment validation acceptance

A deployment of this service is considered acceptable only when all of the following are
demonstrated on the actual target platform:

- Sleep/restart: data survives platform sleep and restart; a restart does not start from a
  fresh image that drops the database.
- Concurrent devices: simultaneous writes from multiple devices converge without lost updates
  (ingest dedupe by `external_id`/digest still holds).
- Backup restore/rollback: a backup restores into a fresh deployment and a migration rolls
  back cleanly.
- Conscious config change: config, identity, and prompt changes happen only by explicit
  redeploy, never by silent runtime mutation.
- Inbound auth: requests without a valid companion token are rejected on state endpoints.
- Logs and secret redaction: provider keys, companion tokens, and prompt text are not
  emitted to logs or metrics.
- Scheduler lease retry: a failed consumer releases or expires its lease and the event is
  retried. Delivery is at-least-once: an acknowledge can be lost or arrive after a lease
  expires, so a consumer must deduplicate by event ID and must not assume a message is never
  delivered twice.
- Provider passthrough streaming: streaming and non-streaming chat completions proxy through
  with correct assistant ingestion.

## 8. Limitations

- No hosted runtime has been exercised. Evidence is the local pytest suite passing, Ruff, and
  installed-wheel checks in a scratch environment.
- Cloudflare statements reflect official docs at research time. Persistence snapshots were
  planned, not guaranteed; general availability was not verified for every feature.
- Latency, cost, and product maturity are intentionally not assessed here; they would depend
  on a chosen provider and would go stale quickly.
- This document is an assessment. It does not approve a platform, storage, or runtime change,
  and no code, config, dependency, or infrastructure has been changed for it.

## 9. References

Official sources accepted for this document:

- Cloudflare Containers architecture: https://developers.cloudflare.com/containers/concepts/architecture/
- Cloudflare Containers FAQ: https://developers.cloudflare.com/containers/faq/
- Python Workers, FastAPI support: https://developers.cloudflare.com/workers/languages/python/packages/fastapi/
- Python Workers, standard library and runtime limits: https://developers.cloudflare.com/workers/languages/python/stdlib/
- Python Workers, package support: https://developers.cloudflare.com/workers/languages/python/packages/
- Python Workers, FFI: https://developers.cloudflare.com/workers/languages/python/ffi/
- Cloudflare D1: https://developers.cloudflare.com/d1/
- Cloudflare Durable Objects: https://developers.cloudflare.com/durable-objects/
- Cloudflare Vectorize: https://developers.cloudflare.com/vectorize/
- Cloudflare Cron Triggers: https://developers.cloudflare.com/workers/configuration/cron-triggers/

Repository references: `deploy/companion-gateway.service`, `src/companion_gateway/database.py`,
`src/companion_gateway/api.py`, `src/companion_gateway/semantic.py`,
`src/companion_gateway/proactive.py`, `config.example.json`, `docs/CONFIGURATION_MIGRATION.md`,
`docs/DECISION_MODULE_DESIGN.md`, and the expected `docs/API.md` and `docs/CONFIGURATION.md`.