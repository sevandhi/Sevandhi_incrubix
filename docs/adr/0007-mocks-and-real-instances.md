# ADR 0007: Faithful mocks + real local instances (no real communities)

Status: accepted (Day 0).

## Context
- Must-haves 5, 7 and 8: test posts may go only to our own instances; a simulation mode must run with no real
  accounts; `docker compose up` must start local Lemmy and Ghost for real end-to-end posts.
- Reviewers add a new instance during the review (MH3).

## Decision
- **Real local instances run unmodified in compose:**
  - Lemmy 0.19 and Ghost 6, always;
  - Discourse, only under `--profile full`;
  - PeerTube, only with `-f deploy/compose/peertube.yaml --profile video`.
- They sit on network `ibx_sandbox`, which is `internal: true` and has **no internet route**. A stdlib bootstrap
  turns federation off (and checks that it stayed off), creates communities and keys, and writes dev-only
  credentials to a read-only volume.
- **mock-platforms is one FastAPI app** that auto-mounts `mocks/platforms/routes/*.py`, and every tenant is just a
  path. It provides:
  - fault injection: `X-Sim-Fault` or `POST /_faults`, with timeout after create, 429 + reset, status + token,
    `create_then_<status>` and field rename;
  - request counters, used to prove "one post".
- **The mocks follow recorded real answers.** 49 responses were recorded from our own containers, and parity tests
  compare the mocks with them.
- **`SIMULATION_MODE=true`** rewrites every connection to the mocks with sim-only credentials.
- **Outside simulation, PREFLIGHT refuses any host not in `CERT_OWN_HOSTS`**, and nothing is sent.
- toxiproxy sits in front of the real Lemmy and Ghost, for timeouts and latency.

## Consequences
- Real posts end to end, with zero risk to any community.
- The mocks stay faithful, though not perfect. Unmeasured items are documented: the Discourse 429 body, the real
  Slack and dev.to answers.
- Discourse is heavy (about 2 min, 1.3 GB), so the default compose uses its mock.
