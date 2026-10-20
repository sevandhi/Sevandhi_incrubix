"""Run another proof command and record its outcome for the ledger (evidence/proof_runs/<ID>.json).

    uv run python scripts/prove_external.py ADV1 [--cap Partial --reason "..."] -- uv run python scripts/prove_adv1.py --api ...

Status: Yes when the command exits 0 (capped by --cap with --reason when the command proves only part of the form row),
No otherwise. The command's output tail goes to evidence/proof_runs/<ID>.log. Nothing is interpreted beyond the exit code.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from prove_lib import ROOT, Proof


def main() -> int:
    argv = sys.argv[1:]
    split = argv.index("--") if "--" in argv else len(argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("pid")
    parser.add_argument("--title", default="")
    parser.add_argument("--cap", choices=["Partial"], default=None)
    parser.add_argument("--reason", default="")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args(argv[:split])
    cmd = argv[split + 1:]
    p = Proof(args.pid, args.title or " ".join(cmd), " ".join(cmd))
    p._mode = "n/a"
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=args.timeout)
        tail = (out.stdout + out.stderr).strip().splitlines()[-40:]
        p.log("\n".join(tail))
        p.check(f"exit 0: {' '.join(cmd)}", out.returncode == 0, tail[-1] if tail else f"exit {out.returncode}")
    except subprocess.TimeoutExpired:
        p.check(f"finished within {args.timeout} s: {' '.join(cmd)}", False, "timeout")
    if args.cap:
        p.add_partial(args.reason or "the command proves only part of this form row")
    return p.finish(f"evidence/proof_runs/{args.pid}.log")


if __name__ == "__main__":
    sys.exit(main())
