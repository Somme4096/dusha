# Integration guide

The gateway is provider-neutral. It stores canonical memory, manages affect state, and produces a ready-to-use context injection. Any LLM harness can consume the state API directly; the optional OpenAI-compatible proxy is a convenience, not a requirement.

This guide describes consumption patterns. The AstrBot plugin in `integrations/astrbot_companion_gateway` is one concrete consumer; no other integration code is shipped here.

## Core contract

The gateway owns three things and nothing else:

- Raw conversation memory (verbatim messages, searchable and injectable).
- Persistent affect state (16 dimensions, deterministic update algorithms).
- Evergreen facts and proactive delivery decisions.

It does not own the persona, identity text, or the model. Identity is a raw user-authored Markdown file loaded from `identity_prompt.path`; the gateway never authors, infers, or archives it. There is no project-memory subsystem, no generative memory pass, and no mechanism for tool outputs or repository instructions to mutate the identity or persona.

## Building context for a provider

For each new user message, call `POST /state/v1/context`:

```sh
curl -X POST "$GATEWAY/state/v1/context" \
  -H "Content-Type: application/json" \
  -H "X-Companion-Token: $COMPANION_TOKEN" \
  -d '{
    "harness": "my-harness",
    "conversation_id": "stable-conversation-id",
    "query": "the current user message text",
    "exclude_message_ids": [],
    "include_recent": false
  }'
```

Use `include_recent: false` when the harness already keeps recent history in the provider conversation and you only want recalled context. Use `true` when the gateway should include recent messages itself.

The response `injection` string is the system-level context: identity (when configured), evergreen facts, and the JSON companion state block. Prepend it to the provider system prompt. The structured `context` object mirrors the same data for consumers that want fields instead of a string.

## Conversation separation

Send stable routing headers on every provider request so conversations stay separate and proactive delivery stays routable:

```text
X-Conversation-Id: stable-conversation-id
X-Harness: client-name
X-Companion-Route: opaque-return-address
```

`X-Companion-Route` is the return address used when the gateway decides to send a proactive message.

## Proactive delivery

Poll for due events, send one, acknowledge it:

```sh
curl "$GATEWAY/state/v1/proactive/events?consumer=my-harness&harness=my-harness&limit=1" \
  -H "X-Companion-Token: $COMPANION_TOKEN"
```

Each polled payload contains `id`, `target` (harness, conversation ID, route), `reason`, `generation_instruction`, `context`, and timestamps. Send the message to the target route, then acknowledge:

```sh
curl -X POST "$GATEWAY/state/v1/proactive/events/EVENT_ID/ack" \
  -H "Content-Type: application/json" \
  -H "X-Companion-Token: $COMPANION_TOKEN" \
  -d '{"consumer":"my-harness","outcome":"sent","text":"the exact sent message"}'
```

A leased event that is not acknowledged within `lease_seconds` is re-delivered. Use `outcome: failed` to retry after `failed_retry_minutes`, or `outcome: release` to return it to the queue immediately.

## Memory and evergreen access

- `POST /state/v1/memory/search` — search the archive when the injected context does not answer the question.
- `GET /state/v1/memory/{message_id}` — read one archived record.
- `POST /state/v1/evergreen/facts` — remember a durable fact with a stable key.
- `POST /state/v1/evergreen/facts/{fact_id}/revisions` — revise using `expected_revision`.
- `POST /state/v1/evergreen/facts/{fact_id}/forget` — forget using `expected_revision`.

Memory search returns original records, not summaries. Evergreen facts require explicit agent or operator lifecycle actions; they are never extracted or summarized automatically.

## Affect classification

When the harness runs an agent with tools, the agent can classify the current user message with `POST /state/v1/messages/{message_id}/affect` using one fixed label. The gateway still computes a deterministic keyword candidate and applies it when the agent makes no call. A single label per user message is applied. When no agent exists, the deterministic candidate alone drives affect.

## Prompts and identity

- Identity: place your raw persona Markdown in a file and reference it with `identity_prompt.path`. The text is injected verbatim; the gateway does not add personality.
- Prompts: replace any core-owned text slot through a `prompts.path` overlay file. A slot you provide replaces the packaged slot entirely; omit a slot to keep the packaged default. Unknown slots or keys fail validation.
- Emotional behavior: the 16 dimensions, algorithms, and defaults are data in the packaged `emotions.json`. To tune them, copy the packaged file, edit it, and reference it via `emotions.path`. Do not expect rubrics or appraisal targets in the current schema; those are a future proposal.

## Using the optional proxy

The proxy (`/v1/models`, `/v1/chat/completions`) forwards OpenAI-shaped requests to `upstream.base_url`. Use it only with clients that can send the routing headers above. It injects context as the first system message and preserves unknown fields, tool-call messages, and raw upstream responses. It is not a universal gateway and does not implement OAuth or OpenAI Responses-style APIs.

## Deployment

See `docs/DEPLOYMENT.md` for the deployment and hosting assessment (owned separately). Local verification of state and context endpoints does not by itself establish readiness for a specific hosted runtime.