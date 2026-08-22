"""
Simple name -> provider-class registry. Each provider package (e.g.
app.providers.aws) calls register() on import; app.providers/__init__.py
imports all of them so the registry is populated as soon as anyone does
`import app.providers`.
"""
from app.providers.base import CloudProvider

_REGISTRY: dict[str, type[CloudProvider]] = {}


def register(name: str, provider_cls: type[CloudProvider]) -> None:
    _REGISTRY[name] = provider_cls


def get_provider(name: str) -> CloudProvider:
    """Return a new instance of the provider registered under `name`."""
    try:
        provider_cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"No provider registered for '{name}'. "
            f"Registered providers: {sorted(_REGISTRY.keys())}"
        )
    return provider_cls()


def available_providers() -> list[str]:
    return sorted(_REGISTRY.keys())
