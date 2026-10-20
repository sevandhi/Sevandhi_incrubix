# ADR 0006: Dev.to is certified at L2, never "deleted"

Status: accepted. A clarification question was sent to IncuBrix (docs/CLARIFICATIONS.md).

## Context
- The Forem API v1 has no article DELETE. `PUT /articles/{id}/unpublish` needs a moderator or admin; an author can
  only set `published: false`.
- L3 requires "publish, status and delete tested". Claiming delete would be a false claim, which disqualifies.

## Decision
- The contract `delete` for `devto` answers 404 `METHOD_NOT_SUPPORTED`. The manifest has no `delete` capability.
- `Connector.unpublish_to_draft()` reverts to draft (or uses the admin unpublish on our own Forem mock) and wipes
  the title and body. It returns `deleted: false, unpublished: true` with the warning `PLATFORM_NO_DELETE`.
- **Certification:**
  - Dev.to runs end with `CONFIRM_REMOVED` and `removal_confirmation.json` (`"deleted": false`).
  - The level is **L2**, with the reason "L3 is not claimed".
  - The MH5 audit counts these runs as `unpublished_to_draft`, never as `deleted`.
- `instance.draft_only: true` keeps any real dev.to run as an unpublished draft.

## Consequences
- The form reports Dev.to as "Partial: documented platform limitation".
- The real dev.to API was never called, because no key was provided (D3).
