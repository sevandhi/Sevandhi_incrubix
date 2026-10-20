# ADR 0004: Rules gate order (replay → spec → rules; deterministic checks)

Status: accepted.

## Context
Must-have 4: rules are checked before every post. Two ordering questions came up:
1. A retry of a post that already succeeded must not be blocked by the frequency cap that post used up.
2. A post can break both a format limit and a posting rule.

## Decision
- **The idempotency replay runs before the rules gate and the breaker.** A succeeded key always returns its stored
  result.
- **The spec check (422 `DEST_SPEC_VIOLATION`) runs before the rules gate (409 `DEST_RULE_*`).** This is documented
  in `docs/RULES.md` §8. The rules gate runs inside the claim transaction under a per-destination advisory lock, so
  two concurrent posts cannot both pass a cap.
- **Order of checks inside the engine** (`app/rules/engine.py`): automation → language → blocked terms →
  self-promo → links → disclosure → frequency cap. The strongest action wins
  (block > reschedule > allow_with_changes > allow). The code and message come from the deciding check, and a post
  that will be blocked is never rescheduled.
- **The rules are deterministic code.** An LLM never judges a post.
- **Publications through `/api/v1/publications`:**
  - The rule check comes before "not certified", so a promotional post to an uncertified no-promo row still
    answers `DEST_RULE_SELF_PROMO`.
  - `allow_with_changes` (for example, adding a missing AI disclosure) is applied and the spec is re-checked.

## Consequences
- The answers are predictable: `docs/RULES.md` is a decision table for predict-and-run.
- Rule safety: 113 cases, 0 missed (`evidence/metrics/rule_safety.csv`).
- The promo detector is conservative on purpose. A genuine question containing a CTA word is blocked on a no-promo
  destination, and the block names the word that fired.
