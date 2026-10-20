# Vision: Destination Intelligence

**One line:** a distribution network that certifies itself, heals itself and learns. Destination number 41 costs one
catalogue row and one certification run, not engineering time, so the network grows through operations.

```
catalogue → rules → tailor → publish → metrics → recommend → recertify
    ↑                                                            │
    └────────────── demote / exclude / alert / restore ──────────┘
```

## What is shipped (in this repo)

| Piece | Where | Why it matters to IncuBrix |
|---|---|---|
| Live trust levels L0–L4, with automatic demotion and exclusion | `app/certification/levels.py`, `app/recert/` | A destination that stops working is excluded before anything is posted to it |
| Self-heal recertification after an open breaker or a schema change | `app/recert/scheduler.py` | A platform change becomes a demotion and an alert, not silent failures |
| Rules drift: the rules page hash changes, so a person must re-confirm | `app/recert/drift.py` | Communities change their rules; posting pauses until someone re-confirms |
| Rules engine as a compliance firewall | `app/rules/` | One careless post can get IncuBrix banned; every post is checked first |
| Recommender that reads fetched metrics as past results | `app/recommender/`, `app/metrics/` | The base for "where should this go"; it feeds a performance view |
| Network health API and page | `GET /api/v1/network/health`, `/ui/network` | One view of trust, last certification, failure streak, rules version and yield |

## What it would take to grow (roadmap, not claimed)

1. **Human review loops as product features.** The confirm UI for rules, the label review CLI and the catalogue
   spot-check become an operator console, with an audit trail (who confirmed what, and when).
2. **Learning from outcomes.**
   - A moderator removal or a community rejection (already recorded as an outcome) automatically tightens that
     destination's cap or demotes it.
   - Cross-creator yield priors improve the recommender's past-results factor.
3. **Discovery.** Propose new destinations from links in high-yield communities. Each one still goes through
   import → rules capture → human confirmation → certification.
4. **More connectors from the template:** Hashnode (GraphQL), Kit/Mailchimp broadcasts, WriteFreely, Matrix rooms.
   Each one is a folder plus a mock route plus a certification run.

## What breaks at 10× and 100×, and the fix

| Scale | Pressure | Fix |
|---|---|---|
| 10× publications | one Postgres queue, per-process pacing | partition the queue per connector; move per-host pacing into a shared Postgres token table |
| 10× destinations | recertification load, which is rate-limited by our own concurrency (`RECERT_CONCURRENCY`) | spread runs by jitter (already done); certify by sample on very large homogeneous groups |
| 100× | the lookup windows for reconciliation (newest 50/25/100 posts) | store the platform id at the moment of the timeout where the platform returns it early; per-connector search APIs |
| Hosted LLM use | free-tier quotas | the chain already falls back; cache rules-page hashes so unchanged pages are never re-sent |

## Cost (see REPORT.md §6)

- **$0 per 1,000 publications** today. Platform APIs are free, the posting path makes no LLM call, and the demo
  runs on free tiers.
- The variable cost to watch is LLM tokens for rules capture: one call per destination per rules change, not per
  post.
