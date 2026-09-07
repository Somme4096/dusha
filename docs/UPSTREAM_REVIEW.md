# Upstream review

Review date: 2026-09-06

The review used repository source, tests, manifests, licenses, and current AstrBot platform code. README claims did not decide the selection.

## Selected work

### Omemo

- Repository: `OmniDimen/omemo`
- Inspected commit: `5e6db375a19aea491b9b0a88111e829f222f0b07`
- License: Apache-2.0
- Useful code: OpenAI chat completion forwarding, streaming support, tool field preservation, and an AstrBot setup path.
- Required change: Omemo stores generated memory summaries in a JSON file. It does not preserve every original message as canonical data. This project keeps its proxy boundary and replaces the memory layer with SQLite raw history plus disposable FTS5 and child-vector indexes.

### Drivesoid v2.0.0

- Repository: `A1batr055/Drivesoid`
- Inspected tag and commit: `v2.0.0`, `09d34ad1415e368f983c5cb52a43219dfa07bda7`
- License: MIT. Drivesoid changed to CC BY-NC-SA 4.0 after v2.0.0.
- Useful code: 16 drive values, distinct fear labels, fast state plus slow mood, state-dependent delta scaling, habituation, and elapsed-time accumulation.
- Required change: upstream uses a second service, Node.js, random display noise, sleep and body calculations, and an LLM classifier. This project ports the deterministic core to Python, removes random noise and body cycles, and uses explicit event labels plus configurable phrase matching.

## Rejected as direct dependencies

### Paramecium

- Repository: `Shitsuten/paramecium`
- Inspected commit: `7695d520e60bdbecc15086168d64321f2d1e039a`
- Strength: raw archive plus derived Chroma and FTS indexes, with retrieval back to verbatim messages.
- Reason: the repository has no license. Its code also hardcodes local paths, separate services, Chroma, and a specific companion deployment. The canonical raw history requirement comes from this project specification and uses an independent implementation.

### Imprint Memory

- Repository: `Qizhan7/imprint-memory`
- Inspected commit: `f5a210cbe50ab78055305ba430b5fdf7632e6ed2`
- Strength: SQLite conversation log, FTS5, optional vectors, and parent-child retrieval that returns original messages.
- Reason: AGPL-3.0 and a larger Claude-specific task, hook, and message-bus stack. Its raw-log retrieval pattern confirmed the design but no code was copied.

### Ombre-Brain

- Repository: `P0luz/Ombre-Brain`
- Inspected commit: `bad26233820e6f7abaafcd7339440b186d15b5ea`
- Strength: mature hybrid recall and MCP access.
- Reason: about 500 files, model and vector dependencies, and features outside memory and affect state.

### Eventide

- Repository: `chuli1122/Eventide`
- Inspected current source on 2026-09-06.
- Strength: tested Python state transitions and persistence-friendly serialization.
- Reason: PolyForm Noncommercial license and a physiological body-cycle model that does not fit this service.

## AstrBot and Discord

- AstrBot commit: `6b848090525a0eebe622a9d46b032a6c5148c4a3`
- AstrBot exposes `on_llm_request`, `on_llm_response`, `llm_generate`, and `Context.send_message` to plugins.
- `Context.send_message` dispatches through the matching platform instance.
- The current Discord adapter implements `send_by_session`, resolves the channel ID, and calls the Discord event sender. The included plugin uses that generic path and declares Discord support.
- The reviewed third-party proactive plugin omits Discord from its declared support list, so this project does not depend on it.
