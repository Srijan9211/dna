"""Command-line configuration tool.

    python -m dna.config_cli validate
    python -m dna.config_cli show [--section prodtrack] [--provenance]
    python -m dna.config_cli handshake [--candidate FILE] [--smoke-email EMAIL] [--apply] [--json]
    python -m dna.config_cli history | export [-o FILE] | import FILE | rollback N

Runs against the same files and environment as the server (``DNA_CONFIG_DIR`` and the
environment), so ``validate`` in CI proves the exact configuration that will deploy,
and ``handshake`` proves it works against the real tracker.  Exit status is 0 on
success and 1 on any invalid configuration or failing check, for use in pipelines.

``history``, ``export``, ``import`` and ``rollback`` act on the shared revision store,
so they need ``DNA_CONFIG_PERSIST=true`` (otherwise there is nothing to share).
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any, Optional, Sequence

import yaml

from dna import configsys
from dna.configsys import ConfigError

_MARK = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "skip"}


def _actor() -> str:
    return f"cli:{getpass.getuser()}"


def _print_error(exc: ConfigError) -> None:
    print("Invalid configuration:", file=sys.stderr)
    for issue in exc.issues:
        print(f"  - {issue}", file=sys.stderr)


def _need_persistence() -> bool:
    if configsys.get_config().get("admin").persist:
        return True
    print(
        "This needs the shared revision store: set DNA_CONFIG_PERSIST=true "
        "(and MONGODB_URL) so changes are recorded and visible to every instance.",
        file=sys.stderr,
    )
    return False


def cmd_validate(_: argparse.Namespace) -> int:
    configsys.get_config()  # raises ConfigError listing every problem
    print("Configuration is valid.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cfg = configsys.get_config()
    data: dict[str, Any] = cfg.effective()
    if args.section:
        if args.section not in data:
            print(
                f"Unknown section '{args.section}'. Sections: {', '.join(data)}",
                file=sys.stderr,
            )
            return 1
        data = {args.section: data[args.section]}
    out: dict[str, Any] = {"config": data}
    if args.provenance:
        prefix = f"{args.section}." if args.section else ""
        out["provenance"] = {
            k: v for k, v in cfg.provenance().items() if k.startswith(prefix)
        }
    print(yaml.safe_dump(out, sort_keys=False, default_flow_style=False).rstrip())
    return 0


def cmd_handshake(args: argparse.Namespace) -> int:
    from dna.prodtrack_providers.handshake import check_candidate

    candidate: dict[str, Any] = {}
    if args.candidate:
        with open(args.candidate, encoding="utf-8") as fh:
            candidate = yaml.safe_load(fh) or {}
    try:
        report = check_candidate(candidate, smoke_email=args.smoke_email)
    except ValueError as exc:  # ConfigError is a ValueError
        if isinstance(exc, ConfigError):
            _print_error(exc)
        else:
            print(f"Cannot run the handshake: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(f"{report.connector} (contract {report.contract})")
        for check in report.checks:
            print(f"  [{_MARK[check.status]}] {check.name:18} {check.message}")
        for s in report.suggestions:
            hint = f" -> try {', '.join(s.options[:3])}" if s.options else ""
            print(f"  suggest: {s.path}: {s.problem}{hint}")
        print("RESULT:", "OK" if report.ok else "FAILED")

    if args.apply:
        if not report.ok:
            print("Not saved: the handshake has failing checks.", file=sys.stderr)
            return 1
        if not _need_persistence():
            return 1
        result = configsys.apply_overrides(
            candidate, actor=_actor(), note="handshake (cli)"
        )
        print(
            f"Saved as revision {result.version}; changed: {', '.join(result.changed) or 'nothing'}"
        )
    return 0 if report.ok else 1


def cmd_history(_: argparse.Namespace) -> int:
    if not _need_persistence():
        return 1
    for rev in configsys.history(50):
        when = (
            __import__("datetime")
            .datetime.fromtimestamp(rev.at)
            .isoformat(timespec="seconds")
        )
        print(
            f"v{rev.version:<4} {when}  {rev.actor:<24} {rev.note or '-'}  [{', '.join(rev.changed)}]"
        )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    text = yaml.safe_dump(configsys.export_overrides(), sort_keys=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {args.output}")
    else:
        print(text.rstrip())
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    if not _need_persistence():
        return 1
    with open(args.file, encoding="utf-8") as fh:
        document = yaml.safe_load(fh)
    result = configsys.import_overrides(document, actor=_actor())
    print(
        f"Imported as revision {result.version}; changed: {', '.join(result.changed) or 'nothing'}"
    )
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    if not _need_persistence():
        return 1
    result = configsys.rollback(args.version, actor=_actor())
    print(f"Rolled back to v{args.version} as revision {result.version}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dna.config_cli", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="load and validate the configuration").set_defaults(
        func=cmd_validate
    )
    p = sub.add_parser(
        "show", help="print the effective configuration (secrets masked)"
    )
    p.add_argument("--section")
    p.add_argument(
        "--provenance", action="store_true", help="show where each value came from"
    )
    p.set_defaults(func=cmd_show)
    p = sub.add_parser(
        "handshake", help="check the tracker configuration against the real tracker"
    )
    p.add_argument(
        "--candidate", help="YAML file of overrides to try (not applied unless --apply)"
    )
    p.add_argument(
        "--smoke-email", help="also walk this user's projects, playlists and versions"
    )
    p.add_argument(
        "--apply", action="store_true", help="save the candidate if every check passes"
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_handshake)
    sub.add_parser("history", help="list recorded changes").set_defaults(
        func=cmd_history
    )
    p = sub.add_parser("export", help="write the runtime overrides to a file")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("import", help="replace the runtime overrides from a file")
    p.add_argument("file")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("rollback", help="re-apply an older revision")
    p.add_argument("version", type=int)
    p.set_defaults(func=cmd_rollback)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        _print_error(exc)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
