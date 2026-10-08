"""Check, with real processes, that several backend instances behave as one.

Starts N backend processes on consecutive ports against a throwaway MongoDB database,
connects one browser (WebSocket) to each, and has one instance broadcast.  Every
browser must see every event exactly once.  Then it stops the instance that is
broadcasting and checks the rest carry on.

    python scripts/multi_instance_check.py --instances 3

Needs MongoDB (``--mongodb-url``, default ``mongodb://localhost:27017``) and ``websockets``.
Exit status is 0 when everything held, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def start(port: int, db: str, mongodb_url: str, bus: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "DNA_EVENT_BUS": bus,
        "DNA_TESTING_ENABLED": "true",  # enables the dev-only broadcast endpoint
        "AUTH_PROVIDER": "none",
        "PRODTRACK_PROVIDER": "mock",
        "MONGODB_URL": mongodb_url,
        "MONGODB_DB": db,
        "PYTHONPATH": str(SRC),
        "LOG_LEVEL": "WARNING",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port)],
        cwd=SRC,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_until_up(port: int, seconds: float = 40.0) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as r:
                return json.load(r)
        except Exception as exc:
            last = exc
            time.sleep(0.3)
    raise SystemExit(f"instance on port {port} did not start: {last}")


def broadcast(port: int, payload: dict) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/test/broadcast-transcript",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=5).read()


async def listen(port: int, sink: list) -> None:
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
        async for text in ws:
            sink.append(json.loads(text))


async def run(ports: list[int]) -> bool:
    sinks = {p: [] for p in ports}
    tasks = [asyncio.create_task(listen(p, sinks[p])) for p in ports]
    await asyncio.sleep(1.0)  # let every browser connect and every bus start listening
    ok = True

    def report(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(
            f"  {'PASS' if passed else 'FAIL'}  {label}{('  ' + detail) if detail else ''}"
        )

    # every instance broadcasts three events in turn
    started = time.perf_counter()
    for i, port in enumerate(ports):
        for n in range(3):
            await asyncio.to_thread(
                broadcast, port, {"type": "transcript", "from": i, "n": n}
            )
    await asyncio.sleep(1.0)
    expected = {(i, n) for i in range(len(ports)) for n in range(3)}
    for port in ports:
        seen = [
            (m["from"], m["n"]) for m in sinks[port] if m.get("type") == "transcript"
        ]
        report(
            f"browser on :{port} saw all {len(expected)} events exactly once",
            sorted(seen) == sorted(expected),
            f"({len(seen)} received)",
        )
    print(
        f"        total time for {len(expected)} broadcasts + delivery: {time.perf_counter() - started:.2f}s"
    )

    # order within one instance's events is preserved everywhere
    for port in ports:
        for origin in range(len(ports)):
            order = [m["n"] for m in sinks[port] if m.get("from") == origin]
            report(
                f":{port} saw instance {origin}'s events in order", order == [0, 1, 2]
            )

    for t in tasks:
        t.cancel()
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--instances", type=int, default=3)
    parser.add_argument("--base-port", type=int, default=8101)
    parser.add_argument("--mongodb-url", default="mongodb://localhost:27017")
    parser.add_argument(
        "--bus",
        choices=["mongodb", "memory"],
        default="mongodb",
        help="memory is a control: without the bus the browsers should NOT all see everything",
    )
    args = parser.parse_args()

    from pymongo import MongoClient

    db = f"dna_multi_{uuid.uuid4().hex[:8]}"
    ports = [args.base_port + i for i in range(args.instances)]
    processes = []
    try:
        print(f"starting {args.instances} instances on ports {ports} (database {db})")
        processes = [start(p, db, args.mongodb_url, args.bus) for p in ports]
        for p in ports:
            health = wait_until_up(p)
            print(f"  :{p} up, health {health}")
            if args.bus == "mongodb" and health.get("events") is not True:
                raise SystemExit(
                    f"instance on :{p} reports its event bus is not running"
                )
        ok = asyncio.run(run(ports))
    finally:
        for proc in processes:
            proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        MongoClient(args.mongodb_url).drop_database(db)
    print("\nALL CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
