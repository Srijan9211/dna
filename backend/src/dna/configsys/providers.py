"""Provider registries.

Each pluggable subsystem (prodtrack, auth, llm, storage, transcription) owns a
:class:`ProviderRegistry`.  Built-ins are registered by dotted path and imported
lazily, so selecting ``mock`` never imports the ShotGrid SDK.  A studio adds a
provider without forking DNA by publishing a Python package that declares an
entry point in the subsystem's group:

    [project.entry-points."dna.prodtrack"]
    ftrack = "my_studio.dna_ftrack:FtrackProvider"

Resolution order for a name: explicitly registered callables, built-ins, then
installed entry points.  The class is resolved on every call (never cached), so
tests and hot-patching that replace the class in its module keep working.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from importlib import metadata
from typing import Any, Optional, Union

Target = Union[str, Callable[..., Any]]


class ProviderRegistry:
    def __init__(
        self,
        *,
        group: str,
        label: str,
        builtins: Optional[dict[str, str]] = None,
        quote_name_in_error: bool = False,
    ):
        """
        Args:
            group: Entry-point group for third-party providers (``dna.llm``).
            label: Used in the error for an unknown name, e.g. ``"LLM provider"``
                gives ``Unknown LLM provider: foo``.
            builtins: ``{name: "package.module:Class"}`` resolved lazily.
        """
        self.group = group
        self.label = label
        self._builtins: dict[str, str] = dict(builtins or {})
        self._explicit: dict[str, Callable[..., Any]] = {}
        self._quote = quote_name_in_error

    def register(self, name: str, target: Target) -> None:
        """Register a provider, replacing any built-in of the same name."""
        if isinstance(target, str):
            self._builtins[name] = target
            self._explicit.pop(name, None)
        else:
            self._explicit[name] = target

    def names(self) -> list[str]:
        found = list(self._builtins)
        found += [n for n in self._explicit if n not in self._builtins]
        found += [n for n in self._entry_points() if n not in found]
        return found

    def resolve(self, name: str) -> Callable[..., Any]:
        """Return the provider class/factory for ``name``.

        Raises:
            ValueError: ``Unknown <label>: <name>`` when nothing matches.
        """
        if name in self._explicit:
            return self._explicit[name]
        if name in self._builtins:
            module_name, _, attr = self._builtins[name].partition(":")
            return getattr(importlib.import_module(module_name), attr)
        entry = self._entry_points().get(name)
        if entry is not None:
            return entry.load()
        shown = f"'{name}'" if self._quote else name
        raise ValueError(f"Unknown {self.label}: {shown}")

    def create(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self.resolve(name)(*args, **kwargs)

    def _entry_points(self) -> dict[str, metadata.EntryPoint]:
        try:
            return {ep.name: ep for ep in metadata.entry_points(group=self.group)}
        except Exception:  # pragma: no cover - broken metadata must not break startup
            return {}


# ── the five DNA subsystems ──────────────────────────────────────────────────

storage = ProviderRegistry(
    group="dna.storage",
    label="storage provider",
    builtins={"mongodb": "dna.storage_providers.mongodb:MongoDBStorageProvider"},
)

transcription = ProviderRegistry(
    group="dna.transcription",
    label="transcription provider",
    builtins={"vexa": "dna.transcription_providers.vexa:VexaTranscriptionProvider"},
)

llm = ProviderRegistry(
    group="dna.llm",
    label="LLM provider",
    builtins={
        "anthropic": "dna.llm_providers.anthropic_provider:AnthropicProvider",
        "gemini": "dna.llm_providers.gemini_provider:GeminiProvider",
        "openai": "dna.llm_providers.openai_provider:OpenAIProvider",
        "custom": "dna.llm_providers.custom_provider:CustomProvider",
    },
)

auth = ProviderRegistry(
    group="dna.auth",
    label="auth provider",
    builtins={
        "none": "dna.auth_providers.noop_auth_provider:NoopAuthProvider",
        "google": "dna.auth_providers.google_auth_provider:GoogleAuthProvider",
        "shotgrid": "dna.auth_providers.shotgrid_sso:ShotGridSSOProvider",
        "ayon": "dna.auth_providers.ayon_login:AyonLoginProvider",
    },
    quote_name_in_error=True,
)

prodtrack = ProviderRegistry(
    group="dna.prodtrack",
    label="production tracking provider",
    builtins={
        "mock": "dna.prodtrack_providers.mock_provider:MockProdtrackProvider",
        "shotgrid": "dna.prodtrack_providers.shotgrid:ShotgridProvider",
        "ayon": "dna.prodtrack_providers.ayon:AyonProvider",
    },
)
