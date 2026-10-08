"""Generate docs/CONFIGURATION_REFERENCE.md from the settings registry.

    python scripts/generate_config_reference.py          # rewrite the file
    python scripts/generate_config_reference.py --check  # exit 1 if it is stale

The reference is built from the same declarations the server validates against, so it
cannot disagree with the code; a test runs ``--check`` to keep it that way.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from pydantic_core import PydanticUndefined  # noqa: E402

from dna.configsys import default_registry  # noqa: E402
from dna.configsys.meta import iter_fields, meta  # noqa: E402

TARGET = ROOT / "docs" / "CONFIGURATION_REFERENCE.md"
LOCKED = {"auth", "storage", "server", "admin"}


def _default(info) -> str:
    if info.default is not PydanticUndefined and info.default is not None:
        value = info.default
        if value == "":
            return "_unset_"
        return (
            "`" + (str(value).lower() if isinstance(value, bool) else str(value)) + "`"
        )
    if info.default_factory is not None:
        try:
            value = info.default_factory()
        except Exception:  # pragma: no cover - defensive
            return ""
        if isinstance(value, (list, dict)) and not value:
            return "_empty_"
        return ""  # a nested group: its fields are listed on their own
    return "_unset_"


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def render() -> str:
    out = [
        "# Configuration reference",
        "",
        "_Generated from the settings registry by `backend/scripts/generate_config_reference.py`. "
        "Do not edit by hand._",
        "",
        "Every setting can be given in a YAML file, as the environment variable shown, or "
        "as `DNA__<SECTION>__<PATH>` (for example `DNA__SERVER__LOG_LEVEL`). See "
        "[CONFIGURATION.md](CONFIGURATION.md) for how the layers combine.",
        "",
        "* **Restart** settings are read at startup; the others apply live.",
        "* **Secret** settings accept only a reference such as `${env:NAME}`, never the value.",
        f"* Sections {', '.join(f'`{n}`' for n in sorted(LOCKED))} are read at startup "
        "and cannot be changed through the admin API.",
        "* **Project** scope means the setting may also be overridden for one project.",
        "",
    ]
    for spec in default_registry.sections():
        note = " _(locked: files and environment only)_" if spec.name in LOCKED else ""
        out += [f"## `{spec.name}`{note}", ""]
        rows = []
        for path, info in iter_fields(spec.model):
            m = meta(info)
            if hasattr(info.annotation, "model_fields"):
                continue  # a nested group: its own fields are listed individually
            flags = []
            if m.get("secret"):
                flags.append("secret")
            if m.get("reload") == "restart":
                flags.append("restart")
            if "project" in (m.get("scope") or []):
                flags.append("project")
            rows.append(
                "| `{}` | {} | {} | {} | {} |".format(
                    ".".join(path),
                    f"`{m['env']}`" if m.get("env") else "",
                    _default(info),
                    ", ".join(flags),
                    _cell(info.description or ""),
                )
            )
        if rows:
            out += [
                "| Setting | Environment variable | Default | Flags | Notes |",
                "|---|---|---|---|---|",
                *rows,
                "",
            ]
        else:
            out += ["_No settings._", ""]
        extra = sorted(spec.legacy_env_names)
        if extra:
            out += [
                "Also read from the environment (per-profile aliases, mapped onto "
                "`profiles.<name>.*`): " + ", ".join(f"`{n}`" for n in extra) + ".",
                "",
            ]
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current.replace("\r\n", "\n") != text:
            print(
                "docs/CONFIGURATION_REFERENCE.md is out of date. Run "
                "`python backend/scripts/generate_config_reference.py`.",
                file=sys.stderr,
            )
            return 1
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
