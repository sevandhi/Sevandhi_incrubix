# Labelling rubric for the recommender evaluation (OUT1)

This rubric decides whether one **destination** is a good place for one **content item**. The labels feed
`precision@10` (P@10) in `evidence/levels/out/OUT1_precision.json`.

- Content items: `catalogue/labels/content_items.yaml`. There are 20: 14 on-topic items across the six audiences,
  3 off-topic items and 3 non-English items.
- Candidate destinations: the real seed catalogue `catalogue/seeds/seed_v1.csv` (175 rows).
- Labels: `catalogue/labels/labels.csv`, one row per (item, destination) pair in the pool.

The rubric was written **before** any label and before the recommender's weights were tuned.

## 1. The question

> If IncuBrix posted this item at this destination, through whatever route the destination supports (API, feed or a
> person posting an assisted pack), would it be a good placement?

A good placement meets all four tests below. The label is `1` (yes) only if all four pass, otherwise `0` (no).

| # | Test | Passes when |
|---|---|---|
| T1 | **Topic** | The destination's stated scope (name, category, tags, rules text) covers the item's main subject, or its members would clearly find the item useful. A moderator would not remove it as off-topic. |
| T2 | **Language** | The item's language is one the destination uses (`language` column). `multi` counts as any language. |
| T3 | **Format** | The destination takes this kind of item (see §2). |
| T4 | **Stated rules** | No stated rule forbids this item (see §3). An unknown rule is **not** a reason to say no. |

## 2. Format (T3)

| Destination surface | Takes |
|---|---|
| community, forum, tag, workspace (Slack), link-aggregator, blog-platform, publication | posts, articles, links, and videos shared as links |
| PeerTube `instance`, Dailymotion `video-platform` | a **video** item only: `content_type` `video` or `video_link`, or a video in `media` |
| `launch`, `directory`, `curated-list`, `marketplace`, tool `newsletter` | a **product** announcement only, meaning an item that presents a product or tool people can use (it is promotional). The product must fit the site's scope: AI-tool directories need an AI product; DevHunt needs a developer tool. |
| `feed-ingest`, `ping-api` (daily.dev source suggestion, Podcast Index, Kagi Small Web, SmartNews, IndexNow, WebSub hubs, Ping-o-Matic) | a whole feed or site, never one item. Label `0` for every item. |
| `tips` (Hackaday tips line) | a hardware or maker hack |

## 3. Stated rules (T4)

Use the `self_promo_allowed`, `disclosure_required` and `automation_allowed` columns, the rules text (`frequency_cap`,
`link_policy`, `rules_quote`) and the `notes`.

- **Promotional item** (`is_promotional: true`) to a destination that says no self-promotion, no ads or no
  solicitation → `0`.
  - "Restricted" promotion, such as "only in #community-promotion" or "ask the mods first", → `1` if the other tests
    pass. Write the condition in the note.
  - Vendor forums whose ToS forbids "advertisements … or other solicitations" also forbid a launch post.
- **AI-generated item** (`ai_generated: true`) to a destination that bans AI-generated or machine-generated content →
  `0`. A destination that only asks for disclosure or an `[AI]` tag passes, because the pipeline adds the disclosure.
- **Explicit exclusions** in the rules text → `0`. Examples: "Don't post links to your articles", "No external video
  links", "news under 14 days old" (a tutorial is not news), "Corporate and personal blogs are not eligible", "art or
  music focus only".
- **Automation** does not matter here. A destination that forbids bots is reached through the assisted route, where a
  person posts. That is a delivery question, not a relevance question.

## 4. What the label ignores

These change the **order** of good placements, not whether a placement is good:

- audience size, activity level ("3 active users/month") and certification level;
- past engagement results;
- frequency caps that are currently used up (time-dependent);
- the cost of a paid listing.

## 5. Borderline cases

- **General communities** ("Technology", "Videos", general PeerTube instances) → `1` only when the item fits their
  stated scope. For example, a tech-news community wants news, and a general video instance takes any original video.
- **Vendor or tool forums** (n8n, Bubble, PyTorch, Blender Artists, Obsidian…) → `1` only when the item is about that
  tool or a direct use of it.
- **Adjacent audiences** (developers for an AI item, writers for a creator item) → `1` only if the item is clearly
  useful to them as written, not just loosely related.
- When unsure, ask: "Would a regular member of this destination be glad to see this post?" If the honest answer is
  "probably not", label `0` and say why in the note.

## 6. Who labels, and how it is disclosed

| `labeler` value | Meaning |
|---|---|
| `agent-draft` | AI-drafted using this rubric. **This is not a human label.** |
| `human:<name>` | A person reviewed the row and accepted or flipped it with `python -m app.tools.label review`. |

- `agent_label` keeps the original draft after a person reviews the row, so agent–human agreement can be reported.
- `OUT1_precision.json` reports the label status. While any row is still `agent-draft`, it says
  **"agent-drafted labels, pending human review"** and gives the reviewed fraction.
- The Submission Form may say "hand-labelled" only after every row in the pool has been reviewed by a person
  (PROGRESS decision D5).
- The agent drafted the labels without seeing which system retrieved each candidate or at what rank. The pool
  provenance is kept in a separate file (`catalogue/labels/pool.csv`).

## 7. Pooling (TREC style) and its bias

- For each item, the pool is the union of the top 20 from three systems: the recommender (default weights), BM25 only,
  and relevance only.
- After the weights are tuned on the dev split, the tuned recommender's top 10 for every item is added to the pool
  (round 2), so the ranking that is reported is fully judged.
- Pairs outside the pool are **unjudged** and count as not relevant. This favours the pooled systems over any system
  that was not pooled, such as the random baseline. `OUT1_precision.json` reports judged@10 for every system so the
  reader can see how large the effect is.

## 8. Reviewing (for the human)

```
uv run python -m app.tools.label review --reviewer <your-name>               # every agent-draft row, page by page
uv run python -m app.tools.label review --reviewer <your-name> --item c04    # one item
uv run python -m app.tools.label review --reviewer <your-name> --one-by-one  # one row at a time
uv run python -m app.tools.label stats                                       # progress + agent/human agreement
uv run python -m app.recommender.eval                                        # recompute P@10 (a few seconds)
```

In page mode, the screen shows the item, then up to 10 candidates with the draft label and its note.

- Press **Enter** to accept the whole page.
- Type the numbers of the rows to flip (for example `2 7`), then press Enter.
- `s` skips the page, `q` saves and quits.

Every accepted or flipped row gets `labeler=human:<name>` and `reviewed_at`. The file is saved after every page.
At about 45 seconds per page, the ~800 rows take about 1–1.5 hours.
