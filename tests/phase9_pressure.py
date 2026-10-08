"""Manual, removable Phase 9 synthetic load: uv run python tests/phase9_pressure.py.

Only tests the local event/timeline path; no claim of cloud/network/physical latency.
No new process, file, Room connection or database is created.
"""

import argparse
import json
import statistics
import time

from test_phase9_performance import client_for, emit_turn


def run(turns=1000, robots=2):
    clients = [client_for(f"bench-{index}") for index in range(robots)]
    costs = []
    started = time.perf_counter()
    for index in range(turns):
        client = clients[index % robots]
        before = time.perf_counter_ns()
        emit_turn(client, f"bench-{index}", mode="audio" if index % 2 else "text",
                  status="cancelled" if index % 11 == 0 else "completed",
                  speaker=index % 3 == 0)
        costs.append((time.perf_counter_ns() - before) / 1_000_000)
    snapshots = [client.diagnostic_snapshot() for client in clients]
    values = sorted(costs)
    result = {
        "mode": "synthetic_local_events_only",
        "turns": turns, "robots": robots,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "emit_p50_ms": round(statistics.median(values), 4),
        "emit_p95_ms": round(values[max(0, (len(values) * 95 + 99) // 100 - 1)], 4),
        "window_samples": [s["latency_window"]["turns"] for s in snapshots],
    }
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=1000)
    parser.add_argument("--robots", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.turns <= 100_000 or not 1 <= args.robots <= 20:
        parser.error("turns must be 1..100000 and robots 1..20")
    run(args.turns, args.robots)
