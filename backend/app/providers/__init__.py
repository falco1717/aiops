from .base import NormalizedEvent, Provider, RunSpec
from .claude import ClaudeProvider
from .codex import CodexProvider

PROVIDERS: dict[str, Provider] = {
    ClaudeProvider.name: ClaudeProvider(),
    CodexProvider.name: CodexProvider(),
}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"Unknown provider {name!r}. Known: {', '.join(PROVIDERS)}") from None


__all__ = ["PROVIDERS", "get_provider", "Provider", "RunSpec", "NormalizedEvent"]
