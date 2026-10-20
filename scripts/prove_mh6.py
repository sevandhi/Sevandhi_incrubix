"""MH6 proof: the same idempotency key never creates a second post, even after a timeout.

    scripts/prove_mh6.sh

For every publishing connector (discourse, lemmy, devto, ghost, webhook) on mock-platforms:
  * timeout after the platform created the post (X-Sim-Fault: timeout_after_create:15 > PLATFORM_TIMEOUT_S 10 s)
    -> 503 PLATFORM_UNAVAILABLE outcome unknown; retry with the same key -> 200, the same post; replay -> same id;
    the same key with a different body -> 409 IDEMPOTENCY_KEY_REUSED; exactly 1 post on the platform;
  * create, then a 502 from a proxy -> the retry reconciles, 1 post;
  * 8 identical requests at the same time -> 1 post.
In local mode also the REAL Lemmy and Ghost, with toxiproxy holding the answer for 13 s; posts counted on the platform.
Evidence: evidence/must_haves/MH6_timeout_retry.log, evidence/metrics/duplicates_under_retry.csv
"""

from __future__ import annotations

import sys

from prove_lib import Proof, stack_reachable_by_exec
from prove_scenarios import create_then_error, parallel_same_key, timeout_retry

PUBLISHING = ["discourse", "lemmy", "devto", "ghost", "webhook"]


def main() -> int:
    p = Proof("MH6", "Safe retries: one idempotency key never creates a second post, even after a timeout",
              "scripts/prove_mh6.sh")
    mode = p.mode()
    rows = []

    def run(fn, connector: str, *args) -> None:  # noqa: ANN001
        p.section(f"{fn.__name__} {connector} {' '.join(map(str, args))}")
        try:
            row = fn(p, connector, *args)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed row
            row = {"connector": connector, "scenario": fn.__name__, "status": "FAIL", "first": f"{type(exc).__name__}: {exc}"[:300]}
        rows.append(row)
        p.check(f"{connector} {row.get('scenario')} ({row.get('target', 'mock')}): one post, no duplicate",
                row["status"] == "PASS", {k: row.get(k) for k in ("first", "retry", "same_key_other_body", "posts_on_platform")})

    for connector in PUBLISHING:
        run(timeout_retry, connector, "mock")
        run(create_then_error, connector)
        run(parallel_same_key, connector)
    if mode == "local" and stack_reachable_by_exec():
        for connector in ("lemmy", "ghost"):
            run(timeout_retry, connector, "real-local")
    else:
        p.add_partial("the real Lemmy/Ghost timeout runs need local mode and docker exec into the stack; not run here")
    dupes = sum(int(r["duplicates"]) for r in rows if str(r.get("duplicates", "")).isdigit())
    p.check("duplicate posts under all retry tests = 0", dupes == 0 and all(str(r.get("duplicates")) != "unknown" for r in rows), dupes)
    p.write_csv("evidence/metrics/duplicates_under_retry.csv", rows,
                ["connector", "target", "scenario", "trigger", "first", "retry", "replay_same_id", "same_key_other_body",
                 "posts_on_platform", "duplicates", "status"])
    return p.finish("evidence/must_haves/MH6_timeout_retry.log")


if __name__ == "__main__":
    sys.exit(main())
