"""Secret references.

A secret is never written into a config file or the override store.  Instead
the config holds a *reference* that is resolved at load time:

    ${env:NAME}            value of environment variable NAME
    ${env:NAME:-fallback}  ... or ``fallback`` when NAME is unset
    ${file:/run/secrets/x} contents of a file (trailing newline stripped)

Only whole-string references are recognised, so a value that merely contains
``${`` somewhere (a password, a prompt template) is left untouched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

_ENV_REF = re.compile(r"^\$\{env:([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}$")
# A file path may itself contain ':' (Windows drive letters), so it takes
# everything up to the closing brace and supports no ``:-`` fallback.
_FILE_REF = re.compile(r"^\$\{file:([^}]+)\}$")


def is_ref(value: Any) -> bool:
    """True when ``value`` is a whole-string secret reference."""
    return isinstance(value, str) and (
        _ENV_REF.match(value) is not None or _FILE_REF.match(value) is not None
    )


def ref_target(value: str) -> Optional[tuple[str, str]]:
    """``("env", NAME)`` or ``("file", PATH)`` for a reference, else None."""
    env_match = _ENV_REF.match(value)
    if env_match is not None:
        return "env", env_match.group(1)
    file_match = _FILE_REF.match(value)
    if file_match is not None:
        return "file", file_match.group(1)
    return None


def resolve_ref(value: str, environ: Mapping[str, str]) -> str:
    """Resolve one reference.  Raises ``KeyError``/``OSError`` on failure."""
    env_match = _ENV_REF.match(value)
    if env_match is not None:
        name, fallback = env_match.groups()
        found = environ.get(name)
        if found is None or found == "":
            if fallback is not None:
                return fallback
            raise KeyError(f"environment variable {name} is not set")
        return found
    file_match = _FILE_REF.match(value)
    if file_match is not None:
        return Path(file_match.group(1)).read_text(encoding="utf-8").rstrip("\r\n")
    return value


def resolve_refs(
    data: Any,
    environ: Mapping[str, str],
    errors: list[tuple[str, str]],
    path: tuple[str, ...] = (),
) -> Any:
    """Return ``data`` with every reference resolved.

    Failures are appended to ``errors`` as ``(dotted_path, message)`` and the
    reference string is kept, so the caller can report every problem at once.
    """
    if isinstance(data, dict):
        return {
            k: resolve_refs(v, environ, errors, (*path, str(k)))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [
            resolve_refs(v, environ, errors, (*path, str(i)))
            for i, v in enumerate(data)
        ]
    if is_ref(data):
        try:
            return resolve_ref(data, environ)
        except (KeyError, OSError) as exc:
            errors.append((".".join(path), f"cannot resolve {data}: {exc}"))
    return data


def mask(value: Any, placeholder: str = "********") -> Optional[str]:
    """Replacement shown in place of a secret: a placeholder, or None if unset."""
    if value is None or value == "":
        return None
    return placeholder
