# ADR 0003: Write-ahead publish intent + invisible marker (never double-post)

Status: accepted (Day 0; hardened after review).

## Context
Must-have 6: the same idempotency key must never create a second post, even after a timeout. Most long-tail
platforms (Lemmy, Ghost, Dev.to, PeerTube, Slack) accept no idempotency key. A timeout or 5xx after the request was
sent leaves the outcome unknown: the post may exist.

## Decision
- `BaseConnector.publish` (`app/connectors/base.py`) runs in this order:
  1. look up a stored result for the key: replay it, or 409 if the body differs;
  2. spec check;
  3. rate limit and breaker;
  4. **claim**: one short transaction that runs the rules gate and writes a **PENDING** intent row;
  5. the platform call;
  6. mark the intent SUCCEEDED, FAILED, RETRYABLE or **UNKNOWN**.
- Every post carries a deterministic **marker** derived from the key. The forms are:
  - a markdown comment `[//]: # (ibx-…)`
  - a Ghost slug suffix
  - a Discourse `external_id`
  - Slack message metadata
  - the feed guid
  - the webhook `Idempotency-Key`
- A timeout, 5xx or 408 after the request was sent (and a response-schema mismatch) means **UNKNOWN**. The retry
  first calls the connector's `_lookup(marker)`. If the post is found, the intent is completed with it; only if it
  is not found is the post created again (after `IDEMPOTENCY_SETTLE_S`).
- No DB connection is held during the platform call. A fresh PENDING row is the lock: a duplicate waits for it,
  then replays. Its lease is 6 × `PLATFORM_TIMEOUT_S`.

## Consequences
- **0 duplicates** in every retry scenario, including real Lemmy and Ghost behind toxiproxy
  (`evidence/metrics/duplicates_under_retry.csv`).
- Limit: `_lookup` scans a window (newest 50 Lemmy posts, 25 PeerTube videos, 100 Slack messages). An account that
  posts more than that between the timeout and the retry could miss the marker.
- Rejected: an outbox. It solves "publish an event once", not "the remote side may have created it".
