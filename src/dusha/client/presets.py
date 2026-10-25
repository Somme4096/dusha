from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

COMPATIBLE = "@ai-sdk/openai-compatible"
ANTHROPIC = "@ai-sdk/anthropic"
KINDS = {COMPATIBLE: ("chat",), ANTHROPIC: ("messages",), "@ai-sdk/openai": ("chat", "responses")}
BUILTIN = {
    "anthropic": ("https://api.anthropic.com/v1", ANTHROPIC),
    "openai": ("https://api.openai.com/v1", "@ai-sdk/openai"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai", COMPATIBLE),
    "groq": ("https://api.groq.com/openai/v1", COMPATIBLE),
    "mistral": ("https://api.mistral.ai/v1", COMPATIBLE),
    "xai": ("https://api.x.ai/v1", "@ai-sdk/openai"),
    "cerebras": ("https://api.cerebras.ai/v1", COMPATIBLE),
    "togetherai": ("https://api.together.xyz/v1", COMPATIBLE),
    "deepinfra": ("https://api.deepinfra.com/v1/openai", COMPATIBLE),
    "openrouter": ("https://openrouter.ai/api/v1", COMPATIBLE),
    "perplexity": ("https://api.perplexity.ai", COMPATIBLE),
}
BLOCKED = frozenset(
    {"amazon-bedrock", "azure", "azure-cognitive-services", "github-copilot", "google-vertex"}
)
HEADERS = {
    "opencode": {"x-opencode-session": "{conversation_id}"},
    "opencode-go": {"x-opencode-session": "{conversation_id}"},
}
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class Preset:
    id: str
    name: str
    base_url: str
    endpoints: dict[str, str] = field(default_factory=dict)
    auth: str = "bearer"
    env: str = ""
    variables: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)

    def profile(self, key: str, values: dict[str, str]) -> dict[str, Any]:
        def fill(url: str) -> str:
            return VARIABLE.sub(lambda match: values[match[1]], url)

        raw: dict[str, Any] = {"base_url": fill(self.base_url)}
        if self.endpoints:
            raw["endpoints"] = {kind: fill(url) for kind, url in self.endpoints.items()}
        if key:
            raw["key"] = key
            raw["auth"] = self.auth
        if self.headers:
            raw["headers"] = dict(self.headers)
        return raw


def _preset(provider_id: str, provider: dict[str, Any]) -> Preset | None:
    if provider_id in BLOCKED:
        return None
    api, npm = BUILTIN.get(provider_id, (provider.get("api"), provider.get("npm")))
    models = provider.get("models") if isinstance(provider.get("models"), dict) else {}
    routes: dict[str, Counter[str]] = {}
    for model in [*models.values(), {}]:
        override = model.get("provider") if isinstance(model, dict) else None
        override = override if isinstance(override, dict) else {}
        url = override.get("api") or api
        for kind in KINDS.get(override.get("npm") or npm, ()) if isinstance(url, str) and url else ():
            routes.setdefault(kind, Counter())[url.rstrip("/")] += 1
    if not routes:
        return None
    chosen = {kind: urls.most_common(1)[0][0] for kind, urls in routes.items()}
    base = api.rstrip("/") if npm in KINDS and isinstance(api, str) and api else next(iter(chosen.values()))
    endpoints = {kind: url for kind, url in chosen.items() if url != base}
    variables = tuple(dict.fromkeys(VARIABLE.findall(" ".join([base, *endpoints.values()]))))
    env = next((name for name in provider.get("env") or [] if name not in variables), "")
    return Preset(
        id=provider_id,
        name=str(provider.get("name") or provider_id),
        base_url=base,
        endpoints=endpoints,
        auth="x-api-key" if npm == ANTHROPIC else "bearer",
        env=env,
        variables=variables,
        headers=HEADERS.get(provider_id, {}),
    )


def build(catalog: dict[str, Any]) -> dict[str, Preset]:
    found = {}
    for provider_id, provider in catalog.items():
        if isinstance(provider, dict) and (preset := _preset(str(provider_id), provider)):
            found[preset.id] = preset
    return dict(sorted(found.items(), key=lambda item: item[1].name.lower()))
