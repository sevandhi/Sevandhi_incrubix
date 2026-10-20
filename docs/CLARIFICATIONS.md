# Clarifications with IncuBrix

The brief invites questions by reply to the invitation email, and answers are shared with every candidate on the
assessment. This file records what we asked and what we built while waiting.

## Questions drafted on Day 1

These are our own questions, written by us. Status:
<!-- TODO(human): record the date the email was sent, or "not sent" -->

1. **Contract objects.** Could you share a schema or an example for `rendition`, `destination` and `connection`?
   Do methods return the bare result with HTTP 200, and the error envelope with a non-2xx status?
2. **Routes and jobs.** May connector methods be served at `/connector/v1/` and mirrored under `/api/v1/`, and stay
   synchronous? Jobs (202) would be used only for certification, import and batch publishing. Should the
   idempotency key come from the body, the header, or both?
3. **Dev.to.** The Forem API has no article delete. Is certification acceptable as a Forem-faithful mock plus a
   draft-only real run, with delete reported as "not supported by the platform"?
4. **Extra error codes.** May we add codes in the same style, such as `DEST_RULE_SELF_PROMO`,
   `DEST_RULE_LINK_POLICY` and `DEST_RULE_DISCLOSURE`, documented in the README?
5. **Local instances and deletion.** Is a Discourse mock in the default compose acceptable, with the real Discourse
   under an optional profile? Does a Lemmy admin purge, or a Discourse soft delete that answers 404 publicly, count
   as deleted?
6. **Hidden tests.** Will you use our mock fault-injection controls or your own mocks? Is the clean test machine
   x86_64 or arm64?

## Replies

<!-- TODO(human): paste IncuBrix's replies verbatim here, with the date, and list any change made because of them -->
None recorded yet.

## Defaults we built under (until a reply says otherwise)

| Question | Default in the code | Where |
|---|---|---|
| 1 | Explicit Pydantic models. Inbound accepts unknown fields; outbound has exactly the Appendix A keys; success = 200 + bare result; failure = non-2xx + envelope | `app/contract/models.py`, `docs/CONTRACT.md` |
| 2 | `/connector/v1/{key}/{method}`, mirrored at `/api/v1/connector/...`, synchronous. The body `idempotency_key` wins; an `Idempotency-Key` header is used when the body has none, and a mismatch answers 409. Orchestrated flows are 202 jobs | `app/contract/router.py`, `docs/CONTRACT.md` |
| 3 | Dev.to = L2; unpublish to draft; never `deleted: true`; a real run is draft-only (no key was provided, so the real API was never called) | `docs/adr/0006-devto-level-2.md` |
| 4 | Extra `DEST_RULE_*` and service codes in the same UPPER_SNAKE_CASE style, listed in the README; the eight shared codes are unchanged | `app/errors.py`, `README.md` |
| 5 | Discourse mock by default, real Discourse under `--profile full`. Lemmy purge = `deleted`; a soft delete is reported as `soft_deleted`, separately | `docs/CERTIFICATION.md` |
| 6 | Our mocks are documented (`X-Sim-Fault`, `POST /_faults`, `GET /_counts`). Images are multi-arch (amd64 + arm64 checked) | `docs/CONTRACT.md`, `deploy/compose/*.yaml` |
| Missing rules on a contract call (not asked; decided internally as D9) | allowed, with `rules_source=none_supplied` | `docs/adr/0005-d9-no-rules-default.md` |
| Deadline | assumed 22–23 Oct 2026 (10 calendar days from receipt) | to be confirmed from the invitation email |
