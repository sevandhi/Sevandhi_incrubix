# ADR 0005: Contract publish with no rules anywhere (decision D9)

Status: accepted (default chosen after review; may be revisited).

## Context
IncuBrix's contract suite will send bare Appendix A destinations with no `rules` key. Blocking every such call
with `DEST_RULES_UNCONFIRMED` would fail Must-have 1. Inventing rules would be dishonest.

## Decision
- `BaseConnector.resolve_rules` takes the first source it finds, in this order:
  1. inline `destination.rules`;
  2. the Appendix A record fields on the destination (`frequency_cap`, `self_promo_allowed`, …);
  3. our catalogue's **confirmed** rule set (by `destination_id`, else by `destination_key`);
  4. none.
- With **none**, the contract publish is **allowed** after the spec checks. It returns the warning
  `rules_source=none_supplied` (`_meta.warnings`, `_meta.rules_source`, and the log line
  `rules_source_none_supplied`).
- Rules that are supplied are always enforced.
- **`/api/v1/publications` (our own orchestration) is stricter:**
  - it needs confirmed rules, or answers 409 `DEST_RULES_UNCONFIRMED`;
  - it needs a certified destination, or answers 409 `DESTINATION_NOT_CERTIFIED`.
- Rule proposals (from LLM capture, import or research) are never used for posting until a person confirms them.

## Consequences
- The contract suite works with plain Appendix A objects, and the runtime is told plainly that no rules were
  known.
- The safety net for real posting is the publications API plus certification. This is written in
  `docs/CONTRACT.md`.
