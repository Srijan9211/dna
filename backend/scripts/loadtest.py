"""Load test for a running DNA backend.

Drives the endpoints the review screen uses, with a configurable number of concurrent
simulated users, and reports throughput, error rate and latency percentiles per
endpoint.  It is read-only: it never creates, publishes or changes anything.

    python scripts/loadtest.py --base-url http://localhost:8000 \
        --email you@studio.com --users 20 --seconds 30 \
        --p95-budget-ms 1500 --max-error-rate 0.01

It discovers a project, playlist and versions through the API, so it works against
any connector.  With ``--token`` it sends a bearer token (needed when the backend
has authentication on).  Exit status is 1 when a budget is exceeded, so it can gate a
release.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
from collections import defaultdict
from typing import Any, Optional

import httpx


class Stats:
    def __init__(self) -> None:
        self.latencies: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, int] = defaultdict(int)
        self.error_samples: dict[str, str] = {}

    def record(self, name: str, seconds: float, ok: bool, note: str = "") -> None:
        self.latencies[name].append(seconds)
        if not ok:
            self.errors[name] += 1
            self.error_samples.setdefault(name, note)


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


async def timed(
    client: httpx.AsyncClient,
    stats: Stats,
    name: str,
    method: str,
    url: str,
    **kwargs: Any,
) -> Optional[httpx.Response]:
    started = time.perf_counter()
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        stats.record(name, time.perf_counter() - started, False, type(exc).__name__)
        return None
    ok = response.status_code < 400
    stats.record(
        name,
        time.perf_counter() - started,
        ok,
        f"HTTP {response.status_code}" if not ok else "",
    )
    return response


async def discover(
    client: httpx.AsyncClient, email: str, project: Optional[str]
) -> dict[str, Any]:
    """Find a project, a playlist and its versions through the API itself."""
    projects = (await client.get(f"/projects/user/{email}")).raise_for_status().json()
    if not projects:
        raise SystemExit(f"{email} has no projects; nothing to test against.")
    candidates = [p for p in projects if str(p["id"]) == project] or projects
    chosen, playlists = None, []
    for candidate in candidates:  # the first project that has something to review
        playlists = (
            (await client.get(f"/projects/{candidate['id']}/playlists"))
            .raise_for_status()
            .json()
        )
        if playlists:
            chosen = candidate
            break
    if chosen is None:
        raise SystemExit("None of the user's projects has a playlist.")
    playlist = playlists[0]
    versions = (
        (await client.get(f"/playlists/{playlist['id']}/versions"))
        .raise_for_status()
        .json()
    )
    return {"project": chosen["id"], "playlist": playlist["id"], "versions": versions}


async def user_session(
    client: httpx.AsyncClient,
    stats: Stats,
    scenario: dict[str, Any],
    email: str,
    deadline: float,
) -> None:
    """One simulated reviewer: open the playlist, then flick through versions."""
    project, playlist, versions = (
        scenario["project"],
        scenario["playlist"],
        scenario["versions"],
    )
    while time.monotonic() < deadline:
        await timed(client, stats, "projects", "GET", f"/projects/user/{email}")
        await timed(client, stats, "playlists", "GET", f"/projects/{project}/playlists")
        await timed(
            client, stats, "playlist versions", "GET", f"/playlists/{playlist}/versions"
        )
        for _ in range(random.randint(3, 8)):
            if time.monotonic() >= deadline or not versions:
                break
            version = random.choice(versions)
            await timed(client, stats, "version", "GET", f"/version/{version['id']}")
            thumb = version.get("thumbnail")
            if thumb and str(thumb).startswith("/"):
                await timed(client, stats, "thumbnail", "GET", thumb)
            await timed(
                client,
                stats,
                "mention search",
                "POST",
                "/search",
                json={
                    "query": random.choice(["a", "s", "l", "h"]),
                    "entity_types": ["user", "shot", "asset", "version", "task"],
                    "project_id": project,
                    "limit": 10,
                },
            )
            await asyncio.sleep(random.uniform(0.2, 1.0))  # a person reads


def report(
    stats: Stats, seconds: float, p95_budget_ms: float, max_error_rate: float
) -> bool:
    print(
        f"\n{'endpoint':<20}{'reqs':>7}{'rps':>8}{'err%':>7}"
        f"{'p50':>8}{'p95':>8}{'p99':>8}{'max':>8}  (ms)"
    )
    total = errors = 0
    worst_p95 = 0.0
    for name in sorted(stats.latencies):
        values = stats.latencies[name]
        n, bad = len(values), stats.errors[name]
        total += n
        errors += bad
        p95 = percentile(values, 0.95) * 1000
        worst_p95 = max(worst_p95, p95)
        print(
            f"{name:<20}{n:>7}{n / seconds:>8.1f}{100 * bad / n:>7.1f}"
            f"{statistics.median(values) * 1000:>8.0f}{p95:>8.0f}"
            f"{percentile(values, 0.99) * 1000:>8.0f}{max(values) * 1000:>8.0f}"
        )
    for name, sample in stats.error_samples.items():
        print(f"  first error on {name}: {sample}")
    rate = errors / total if total else 1.0
    print(
        f"\ntotal {total} requests, error rate {rate:.2%}, worst p95 {worst_p95:.0f} ms"
    )
    passed = rate <= max_error_rate and worst_p95 <= p95_budget_ms
    print(
        "PASS"
        if passed
        else f"FAIL (budget: error rate <= {max_error_rate:.2%}, "
        f"p95 <= {p95_budget_ms:.0f} ms)"
    )
    return passed


async def main_async(args: argparse.Namespace) -> bool:
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    limits = httpx.Limits(
        max_connections=args.users * 2, max_keepalive_connections=args.users
    )
    async with httpx.AsyncClient(
        base_url=args.base_url, headers=headers, timeout=60.0, limits=limits
    ) as client:
        scenario = await discover(client, args.email, args.project)
        print(
            f"project {scenario['project']}, playlist {scenario['playlist']}, "
            f"{len(scenario['versions'])} versions; {args.users} users for "
            f"{args.seconds}s"
        )
        stats = Stats()
        deadline = time.monotonic() + args.seconds
        started = time.monotonic()
        await asyncio.gather(
            *(
                user_session(client, stats, scenario, args.email, deadline)
                for _ in range(args.users)
            )
        )
        return report(
            stats, time.monotonic() - started, args.p95_budget_ms, args.max_error_rate
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--email", required=True, help="a user with access to a project"
    )
    parser.add_argument("--project", help="project id (default: the user's first)")
    parser.add_argument("--token", help="bearer token, when authentication is on")
    parser.add_argument(
        "--users", type=int, default=10, help="concurrent simulated users"
    )
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--p95-budget-ms", type=float, default=2000.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(main_async(args)) else 1)


if __name__ == "__main__":
    main()
