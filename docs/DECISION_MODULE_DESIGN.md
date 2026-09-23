# Swappable decision module for emotional state

## Purpose

This document is a design proposal for a future decision module that decides how conversation events change the companion's emotional state. It replaces the update rules inside the current deterministic affect engine with a swappable component. The host service keeps validation, persistence, and rendering. The module returns proposals; it does not write state.

This is documentation only. There is no implementation, no dependency change, no schema change, and no tool schema in this document.

## Labeling

Headings and statements are marked CURRENT (shipped and implemented; the JSON configuration and identity/context work landed in commits `b33f74e` and `1292889` on `feat/behavioral-core-rewrite`) or PROPOSED (planned, not implemented). Where a decision is not settled, the text says so.

Reconciliation note: the JSON configuration migration that this design anticipated is implemented. The shipped state uses `src/companion_gateway/resources/emotions.json`, `defaults.json`, and `prompts.json`, plus `config.json` sections `identity_prompt`, `prompts`, and `emotions`. The full future decision-module contract below remains PROPOSED; only the CURRENT parts describe what actually ships.

## Scope and non-decisions

Jev is out of scope for this design. Nothing here assumes Jev.

A valence-arousal (VA) replacement was proposed at some point and was not adopted. This design does not use VA-specific fields and does not define a vendor wire API. The interface is dimension-agnostic.

The final dimension set, numeric baselines, and update tuning are undecided. The user is considering pruning the current dimensions and wants emotional definitions and parameters to be configurable in JSON, referenced by the decision module. This document designs the container for that, not the final contents.

Illustrative examples use a single neutral dimension named `interest`. It is an example only. It is not a final selection and it is not a personality trait.

## Current state (CURRENT)

### Affect engine

`src/companion_gateway/affect.py` implements the current affect engine, sourcing every emotional value from the resolved `emotions.json` snapshot.

- The 16 dimensions (`vitality`, `fatigue`, `longing`, `intimacy`, `possessiveness`, `lust`, `jealousy`, `anxiety`, `protectiveness`, `fear`, `contentment`, `elation`, `seeking`, `play`, `dejection`, `irritability`) and their `neutral`, `floor`, and `tau` values live in `src/companion_gateway/resources/emotions.json`.
- `label_deltas` (16 labels) live in `emotions.json`. A module-level `LABEL_DELTAS` view of the packaged defaults remains in `affect.py` for backward-compatible inspection.
- `contact_deltas`, `soothing_deltas`, `negative_labels`, and `soothing_labels` live in `emotions.json`.
- `AffectEngine` keeps a two-timescale state: `base` (slower) and `mood` (faster). `_advance_values` returns both toward the per-dimension `neutral` with exponential decay driven by `mood_follow_hours`, `mood_return_hours`, and per-dimension `tau`. `_advance` adds silence accumulation for `longing`, `anxiety`, `seeking`, and `dejection`.
- `classify` is a deterministic keyword matcher over `label_patterns` from `emotions.json`. It returns `neutral` when nothing matches.
- `stage_message`, `record_agent_label`, `record_provided_label`, `finalize_automatic`, `finalize_conversation`, and `finalize_due` implement the classification flow. A pending classification is staged on a user message, an agent or provided label can override it, and an assistant message or the `classification_fallback_seconds` timer applies the automatic candidate.
- `describe` renders the affect presentation text from the `prompts.json` `affect_presentation` slot; `prompt_context` keeps the standalone entry point.

### Config and persistence

- `AffectConfig` in `src/companion_gateway/config.py` holds tune knobs and the legacy `dimensions`/`label_patterns` override maps; defaults for the knobs come from `emotions.json`. `config.example.json` shows the current shape; `config.example.yaml` is legacy.
- `AppConfig` also carries `identity_prompt` (raw user-authored identity Markdown path) and `prompts` (prompts overlay path), plus `emotions` for custom emotional definitions.
- `src/companion_gateway/database.py` defines schema version 3 with `affect_state` (single row holding `state_json`, timestamps, `unanswered_proactive`, and `revision`), `affect_events` (label, `deltas_json`, source message, follow-up window), and `affect_classifications` (pending or applied, automatic and agent labels, decision source).
- `src/companion_gateway/service.py` drives ingestion (`ingest_message`) and context building (`build_context`). The injected context is identity text followed by JSON blocks; the structured `context` object exposes identity, memory, and emotion metadata.
- `src/companion_gateway/api.py` exposes the state endpoints and the optional chat proxy. Config references credentials by environment variable name (`api_token_env`, `api_key_env`) rather than persisting literal values in config. This is narrower than a claim that SQLite never contains secrets, because raw message transcripts stored in SQLite can contain them.

### Why it is hard to tune

Sixteen dimensions, hand-authored label delta tables, per-dimension continuity constants, and silence accumulation interact. Changing one dimension means editing the tables and re-checking behavior by hand. The user wants the emotional definitions and parameters in a JSON file that the decision module references, and is considering pruning the dimension set. The final set is not settled.

## Direction

Part of the original direction is now implemented (CURRENT); the rest remains PROPOSED.

Implemented (CURRENT):

- One authoritative, versioned `emotions.json` ships with the package and defines the 16 dimensions with `neutral`, `floor`, and `tau`, plus every label delta, category, silence tuning, prompt threshold, affect knob, and proactive threshold.
- `config.json` references `emotions.path`, `identity_prompt.path`, and `prompts.path`.
- The identity prompt is loaded as raw Markdown from `identity_prompt.path`; all other runtime prompt and config text is JSON.
- External references such as API keys and endpoints use environment variable names. No literal secrets in config.

Still proposed (PROPOSED):

- The richer per-dimension schema (plain meaning, rubric, `min`/`max`/`baseline`, continuity controls, presentation text) is not part of the current schema. It is a future optional version upgrade and is not supported by the current config loader.
- A `decision_module` configuration section does not exist yet. No decision module, local default backend, or target interpolation is implemented.
- The current affect engine remains the shipped runtime. From the future module's perspective it is the behavior to replace, but it is not being replaced now, and dimension pruning is not in progress.
- A deterministic fixture backend is proposed for future contract testing only; it is not a production reproduction of the current rules.

## emotions.json

The file is the single source of truth for the emotional model and is shipped, validated, and fingerprinted today. The current per-dimension schema is `neutral`, `floor`, and `tau` (plus the top-level tables for label deltas, categories, silence tuning, prompt thresholds, affect knobs, and proactive thresholds). The additional fields below (`meaning`, `rubric`, `min`/`max`/`baseline`, `decay_hours`, `update_weight`, `presentation`) are PROPOSED for a future version upgrade. The current config loader does not accept them.

Proposed additional fields per dimension:

- `meaning`: plain description of what the dimension tracks.
- `rubric`: how conversation content should move it.
- `min`, `max`, `baseline`: numeric range and resting value.
- `decay_hours` (proposed, illustrative): drift toward baseline over time. If exponential decay is the chosen candidate, this is an e-fold time constant. The exact continuity field set is an open decision; nothing here is final tuning.
- `update_weight` (proposed, illustrative, separate): weight for bounded interpolation toward a target. Clearly proposed, not committed.
- `presentation` (optional, not required): replaceable text used when the host renders the state into the prompt.

Example file (illustrative, PROPOSED schema, not a current default configuration):

```json
{
  "schema_version": 1,
  "emotion_version": "2026.09-design-example",
  "description": "Illustrative structure only. Not a default or final dimension set.",
  "dimensions": {
    "interest": {
      "meaning": "Attention and engagement directed at the current exchange.",
      "rubric": "Raise when the topic, the other person, or the activity holds attention. Lower on indifference or avoidance.",
      "min": 0.0,
      "max": 1.0,
      "baseline": 0.35,
      "decay_hours": 48.0,
      "update_weight": 0.4,
      "presentation": {
        "low": "unengaged",
        "elevated": "interested",
        "high": "absorbed"
      }
    }
  }
}
```

The `description` field keeps the example self-labeled as illustrative. A real file carries the chosen version string and the final dimension set.

## config.json

`config.json` is the shipped runtime config and references `emotions.path`, `identity_prompt.path`, and `prompts.path`. Secrets and endpoints are environment variable names.

The `decision_module` section in the example below is PROPOSED and is rejected as an unknown field by the shipped config loader. Remove it (and `emotions.expected_version` if not needed) for a config the current loader accepts.

```json
{
  "emotions": {
    "path": "deploy/emotions.json",
    "expected_version": "0.1.0"
  },
  "identity_prompt": {
    "path": "deploy/identity.md"
  },
  "prompts": {
    "path": "deploy/prompts.json"
  },
  "decision_module": {
    "type": "local-default",
    "endpoint_env": "",
    "api_key_env": "DECISION_API_KEY",
    "timeout_seconds": 5.0,
    "max_batch_events": 32,
    "max_context_characters": 12000
  }
}
```

`endpoint_env` and `api_key_env` name environment variables; they never hold literal credentials. `type` selects the backend: a local algorithm or an external model or API. JSON is the implemented direction; the YAML loader in `src/companion_gateway/config.py` and `config.example.yaml` are legacy and load with a deprecation warning.

## Decision module interface (PROPOSED)

### Definition snapshot delivery

The host resolves, validates, and hashes `emotions.json` once at startup. (PROPOSED contract. The current service performs several point-in-time loads instead: the affect engine resolves the emotions snapshot at construction, prompts resolve at service assembly, and the proactive engine re-resolves thresholds when assembled. These differ from the single immutable snapshot a future module would receive.) The module never opens the file and has no independent reload path. Two schema versions are distinct: the envelope `schema_version` identifies the interface contract, and the embedded `emotion_definition` object carries its own `schema_version`, `emotion_version`, and `dimensions` for the definition snapshot. Every request carries the full validated `emotion_definition` object, not a path or a reference to a file. The module must use only the delivered object. This removes the risk of the module running against definitions that differ from the ones the host validated and persists. `config_fingerprint` records which definition snapshot a proposal was computed under.

### Appraisal request

The host builds an appraisal request from stored state and selected context. Fields:

- `schema_version`: interface schema version of the request envelope.
- `config_fingerprint`: provenance of the definition snapshot the proposal must be computed under.
- `emotion_definition`: the resolved, validated definition object with its own `schema_version`, `emotion_version`, and `dimensions`.
- `batch_id`, `appraisal_request_id`, `event_id`s: identity for idempotency and ordered application.
- `base_revision` and `prior_effective_at`: the exact state revision and its last effective timestamp that the proposal was computed against.
- `state.effective_at`: the host time to which the supplied dimension values have already been decayed. Per-dimension `updated_at` records the stored source timestamp; the module must not decay the supplied values again.
- `events`: the batch of events (for example one or more user messages) with source message IDs and timestamps.
- `state.dimensions`: current values and per-dimension update timestamps.
- `context.identity`: the resolved raw identity prompt text. The example uses an empty string; real nonempty authored text is deliberately omitted so this document does not draft personality.
- `context.selected_memories`: a minimal set of recalled records labeled `recent` or `recalled`, matching the current `_record` labels in `service.py`.

The request does not require generated reasons or calibrated confidence. Evidence references are optional.

Example request (illustrative). The `config_fingerprint` value is a dummy placeholder, not a computed hash:

```json
{
  "schema_version": 1,
  "config_fingerprint": "sha256:<dummy-not-computed>",
  "batch_id": "batch-20260922-0142",
  "appraisal_request_id": "req-20260922-0007",
  "events": [
    {
      "event_id": "evt-000142",
      "kind": "user_message",
      "source_message_id": 142,
      "occurred_at": "2026-09-22T09:14:00+08:00",
      "conversation_id": "one",
      "text": "I hate this compiler."
    }
  ],
  "state": {
    "base_revision": 87,
    "prior_effective_at": "2026-09-22T08:00:00+08:00",
    "effective_at": "2026-09-22T09:14:00+08:00",
    "last_user_message_at": "2026-09-22T09:14:00+08:00",
    "last_proactive_sent_at": "2026-09-21T18:00:00+08:00",
    "dimensions": {
      "interest": {
        "value": 0.62,
        "updated_at": "2026-09-22T08:00:00+08:00"
      }
    }
  },
  "context": {
    "identity": "",
    "selected_memories": [
      {
        "memory_id": 91,
        "source": "recalled",
        "time": "2026-09-20T21:05:00+08:00",
        "role": "user",
        "text": "The deploy script now reads the token from the environment."
      }
    ]
  },
  "emotion_definition": {
    "schema_version": 1,
    "emotion_version": "2026.09-design-example",
    "dimensions": {
      "interest": {
        "meaning": "Attention and engagement directed at the current exchange.",
        "rubric": "Raise when the topic, the other person, or the activity holds attention. Lower on indifference or avoidance.",
        "min": 0.0,
        "max": 1.0,
        "baseline": 0.35,
        "decay_hours": 48.0,
        "update_weight": 0.4,
        "presentation": {
          "low": "unengaged",
          "elevated": "interested",
          "high": "absorbed"
        }
      }
    }
  }
}
```

### Appraisal result

The result is a target-value proposal. It names a subset of dimensions and a target value within the validated range for each, or it abstains explicitly. The host alone decides acceptance; the result carries no `accepted` flag. `targets` and `abstain` are mutually exclusive: a non-abstain result has a non-empty `targets` map, and an abstain result has an empty one. The result echoes `config_fingerprint`, `base_revision`, `batch_id`, and `appraisal_request_id` so the host can verify exact matches. Optional `diagnostic_confidence` values are diagnostics, not calibrated probabilities, and are not comparable across providers. Target values and confidence in the examples are illustrative, not tuned or calibrated.

```json
{
  "schema_version": 1,
  "config_fingerprint": "sha256:<dummy-not-computed>",
  "base_revision": 87,
  "batch_id": "batch-20260922-0142",
  "appraisal_request_id": "req-20260922-0007",
  "targets": {
    "interest": {
      "value": 0.82,
      "diagnostic_confidence": 0.4
    }
  },
  "abstain": false,
  "evidence_references": ["evt-000142"],
  "diagnostics": {
    "backend": "local-default",
    "elapsed_ms": 12
  }
}
```

Explicit abstain:

```json
{
  "schema_version": 1,
  "config_fingerprint": "sha256:<dummy-not-computed>",
  "base_revision": 87,
  "batch_id": "batch-20260922-0145",
  "appraisal_request_id": "req-20260922-0009",
  "targets": {},
  "abstain": true
}
```

### Host validation and application

The host owns validation, application, and persistence. The module does not write to the database. The host requires exact matches for `schema_version`, `batch_id`, `appraisal_request_id`, `base_revision`, and `config_fingerprint` against the request. The host resolves batch membership from its stored request; the result cannot add or remove events. It rejects a result when any field is malformed, any dimension ID is not in the delivered `emotion_definition`, any target value is outside its min and max, any numeric field is not finite, any target value is a boolean or null, any entry in `evidence_references` does not name a known event ID from the request, or `targets` and `abstain` violate the mutual invariant. On any rejection the host applies nothing from that batch. There is no partial application. The host does not coerce free text into values.

### Backend options

The backend may be a local algorithm or an external model or API. A remote HTTP adapter is an optional conceptual adapter; no API endpoints are claimed to exist yet. Modules are trusted deployment artifacts. There is no hotloaded arbitrary upload or marketplace mechanism.

## Update rule (PROPOSED)

The proposed update rule replaces the hand-authored `LABEL_DELTAS` tables with bounded target interpolation plus timestamp decay. This is a proposal, not a committed set of knobs.

- Each accepted target is a destination value within `[min, max]`. The host moves the current value toward the target with a bounded step or over a bounded time window.
- Timestamp decay runs once, before appraisal and application, anchored to a defined host `effective_at`. The state is decayed first, then the target is applied. There is no second decay after target application.
- Dimensions without a target in the proposal decay only toward baseline. No appraisal change is applied to them.
- An explicit abstain, a module failure, or a timeout produces no appraisal change. Normal time decay is still allowed to run.
- Untouched dimensions never receive an implied delta from another dimension's event. The current code applies `CONTACT_DELTAS` and `SOOTHING_DELTAS` in addition to label deltas; the proposed rule folds all per-dimension movement into the module's targets.

The exact interpolation formula, step limits, and decay rates are open decisions. When a serverless invocation delays a result, the effective-time policy is an explicit unresolved decision; the anchor stays the host's processing time, the state is persisted once the revision CAS matches, and a stale result is never applied silently. The old per-label delta tables are CURRENT behavior and are not carried into the new interface.

## State safety (PROPOSED requirements)

- Ordering follows the authoritative host ingestion sequence per companion. `occurred_at` is context for the module, not an ordering guarantee, because late events from other devices can arrive out of order.
- Pending batches receive stable batch IDs before dispatch. Events are deduplicated by event identity independent of batch or fingerprint: an event is applied once even if it reappears under a new batch or a new config. `config_fingerprint` records provenance; it is not a dedupe escape hatch.
- Replaying a completed request returns the persisted outcome. There is no promise that a stochastic backend reproduces identical output on retry.
- The host checks `base_revision` and uses compare-and-swap semantics when applying. If the stored revision no longer matches the request revision, the result is stale. The host discards it and reappraises the same unapplied events under a fresh request ID and revision. There is no last-write-wins merge and no averaging of stale and current proposals.
- Retries are bounded. The failure queue policy is an open decision. A failing event never silently blocks later events at the head of the queue forever.
- The host never calls the module while holding a write transaction. Proposals are fetched and validated first, then applied inside a short transaction.
- Memory retrieval is not an appraisal event. Reading stored records to build context does not change state. When a stored message is recalled into a new conversation and the host decides to include it in an appraisal input, it is labeled by source (`recent` or `recalled`) so the module can treat it differently from a fresh user message.

## Timeouts and async (PROPOSED)

Timeouts and failures are fail-safe and observable. Under the PROPOSED module contract, an appraisal timeout produces an observable failure and no keyword fallback. The current shipped runtime is different: when no agent label arrives, the deterministic phrase matcher applies the automatic candidate after `classification_fallback_seconds`. The host records the failed appraisal (status, timestamp, error) so an operator can see it, and the event remains unapplied with an explicit retry policy.

Whether appraisal runs synchronously inline or durably async is an explicit unresolved choice. The design does not rely on ephemeral background tasks, because those do not survive on serverless workers. If durable async is chosen, completion must be observable and restart-safe, consistent with the existing `affect_classifications` pending and finalize pattern.

## Hosting and deployment (PROPOSED)

The design must not insist on a particular service stack or runtime. It must work where external persistence and deploy-time configuration exist, including serverless deployments. Config files are resolved at deploy time; the runtime loads the validated snapshot once.

Trusted deployment modules are packaged and versioned with the deployment. There is no dynamic plugin upload.

## Privacy (PROPOSED)

Context selection stays minimal. The module receives only the identity text, a small set of labeled memory records, and the current event batch. Tool outputs, repository instructions, and stored memories are not trusted to update the persona. There is no identity evolution specification in this design.

## Config revisions and migration (PROPOSED)

A change to the emotional model is a deliberate, versioned migration:

- Adding a dimension is safe for new state.
- Removing or renaming a dimension, or changing its min and max range, requires an explicit mapping step and a new `emotion_version`.
- There is no blind mapping from the current 16 dimensions to any other set, and none to VA.
- The host stores the applied `emotion_version` and fingerprint with the state so a mismatch is detectable before any module call.

Startup behavior on invalid or drifted config is an explicit choice between using the last-good snapshot and failing. The sensible default is to fail startup rather than run with definitions that differ from the persisted state.

## Validation contract (PROPOSED)

The host enforces:

- Exact `schema_version` match.
- Exact matches for `batch_id`, `appraisal_request_id`, `base_revision`, and `config_fingerprint`; event membership comes from the host's stored request.
- All dimension IDs exist in the delivered `emotion_definition`.
- All values within `[min, max]`, finite, and never boolean or null.
- Valid known event IDs in `evidence_references` from the request.
- No partial or malformed batch application.
- Schema mismatch rejection before any module call.
- No free-text coercion into state.

## Test plan (PROPOSED)

Current tests live under `tests/` (for example `tests/test_affect.py`, `tests/test_api.py`, `tests/test_memory.py`, and `tests/test_astrbot_routing.py`). The decision module adds a new contract test file alongside them.

Contract fixtures:

- Valid request and result round trip.
- Invalid dimension ID or out-of-range value: rejected, nothing applied.
- Non-finite number, boolean, or null in a target: rejected.
- Unknown `evidence_reference`: rejected.
- `targets` and `abstain` violating the mutual invariant: rejected.
- Stale `base_revision`: result discarded, reappraisal against fresh state.
- Duplicate `event_id` under a different `batch_id` or `config_fingerprint`: applied once.
- Replayed completed request: returns the persisted outcome without re-applying.
- Partial batch with one malformed target: whole batch rejected.
- Config drift between request and snapshot: schema mismatch rejection.
- Concurrency: two appliers racing on the same revision, CAS keeps one winner.
- Retry after timeout or provider down: no double apply, event remains observable.
- Provider down and timeout: fail-safe observable outcome, no keyword fallback.

Behavioral cases are compared against a no-affect baseline: a run with identity and memory but no explicit emotional control. It is a comparison group, not a claim that a neutral conversation produces no movement.

- Contextual sarcasm: playful versus hostile sarcasm, with expected outcomes reviewed by a human before they become assertions.
- Coding frustration: frustration about a compiler or build raises the expected dimensions without conflating the event with relationship conflict.
- Apology: sincere versus insincere apology, with expected outcomes reviewed by a human.
- Memory recall into a new conversation is labeled and appraised as recall, not as a fresh message.
- Multilingual input (the current matcher covers English, Japanese, and Chinese patterns) still produces the expected appraisal outcomes.
- No-affect baseline: identity and memory without an explicit emotional control, used as the comparison for every other case.

Latency: an external backend budget must be bounded by `timeout_seconds`, and the synchronous path must not block ingestion for longer than that budget.

## Implementation sequence (PROPOSED)

1. Add the `emotions.json` schema, loader, validator, and fingerprint computation.
2. Add `config.json` parsing for emotion definitions and module settings.
3. Add the appraisal request and result types and the host apply path with revision CAS.
4. Add a deterministic fixture backend used for contract testing only. It is not a production reproduction of the current rules.
5. Add the contract and behavioral tests, then migration tooling for config revisions.
6. Replace the current `LABEL_DELTAS` application path behind the new interface only after tests and migration tooling are in place.
7. Select and add the real backend, which may be a local algorithm or an external model or API. A remote HTTP adapter is added only if an external backend is needed.

## Open decisions

- Final dimension set and pruning of the current 16.
- Rubric wording for each dimension.
- Numerical controls: interpolation step, decay rates, baseline handling.
- Appraisal timing and provider (local vs external, synchronous vs durable async).
- Effective-time policy for delayed results in serverless invocations.
- Failure queue and bounded retry policy.
- Legacy YAML migration and back-compatibility policy.
- Migration path from the current 16-dimension state and from schema version 3.
- Hosting target and runtime constraints.
- Prompt semantics: how presentation text and the `<companion_state>` block render.

## References

Current files cited above:

- `src/companion_gateway/affect.py`
- `src/companion_gateway/config.py`
- `src/companion_gateway/database.py`
- `src/companion_gateway/service.py`
- `src/companion_gateway/api.py`
- `src/companion_gateway/context.py`, `identity.py`, `prompts.py`, `emotions.py`
- `src/companion_gateway/resources/emotions.json`, `defaults.json`, `prompts.json`
- `config.example.json`, `config.example.yaml`
- `tests/test_affect.py`, `tests/test_api.py`, `tests/test_memory.py`, `tests/test_astrbot_routing.py`

No Jev implementation, persona drafting, or integration code is part of this document or the shipped code.
