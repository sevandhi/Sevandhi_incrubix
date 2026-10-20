# Seed catalogue v1 (`seed_v1.csv`)

175 candidate destinations for an audience of AI, video, marketing, SaaS, creators and founders.
Every row is a real URL that answered **HTTP 200** to a read-only GET in the final check pass
(2026-10-15 05:56–05:58 UTC; the exact time per row is in `researched_at`). This is a **seed**: rows have not
been through the import pipeline, relevance scoring or human confirmation yet (that is the seed_v2 step below).
Nothing was posted anywhere.

## Method

1. **Discovery** (2026-10-14/15), read-only:
   - **Lemmy**: the `lemmy.world` community search API (`/api/v3/search?type_=Communities`, 57 generic queries such as
     "AI", "video editing", "marketing", "SaaS"), plus `feddit.org` local search. Each community was then checked on
     its home instance with `/api/v3/community?name=…` and its `/c/<name>` page.
   - **Discourse**: known AI/video/no-code/creator/marketing tool forums, each verified as Discourse through
     `/about.json` (user and 30-day activity counts come from there). About 170 hosts were probed; only live,
     relevant forums were kept.
   - **PeerTube**: the joinpeertube instance index API (`instances.joinpeertube.org/api/v1/instances`), then each
     instance's `/api/v1/config/about` (terms, code of conduct) and `/api/v1/config` (signup state).
     Instances with closed signups, staff-only uploads or a 403 were dropped.
   - **dev.to (Forem)**: tag pages, plus the DEV Code of Conduct and the AI-assisted-articles guidelines. Other Forem
     instances were probed, but they are shut down or redirect to dev.to.
   - **Slack, assisted, feed, webhook and Ghost**: web search with generic terms, the Hive Index directory, and direct
     probes of well-known launch sites, directories and submission APIs. Each row's rules page was fetched directly.
2. **Final check pass** (`build_seed.py`, kept outside the repo): every row URL, `rules_url` and quote source was
   fetched again with no cache.
   - Rows that did not return 200 were dropped.
   - Every `rules_quote` was checked to appear verbatim in the fetched source, after collapsing whitespace and
     normalising curly quotes and Markdown bold. All 106 quotes passed. Quotes are 25 words or fewer.
   - For Lemmy and PeerTube the quote source is the JSON API: `/api/v3/community?name=…` (the sidebar shown on the
     `/c/` page) and `/api/v1/config/about` (the text shown on `/about/instance`). The notes say so.
3. **Crawling etiquette**: User-Agent `IncuBrix-assessment-catalogue-research/0.1 (contact: candidate)`.
   robots.txt was checked for every URL and for every redirect hop, with a gap of at least 1.1 s per host.
   Only GET requests were made. No login and no form submission.

## Columns

These are the destination-record fields that can be filled at seed stage. `relevance_score`, `relevance_reason`,
`level` and `last_certified_at` come later, from the catalogue import and certification.

| Column | Meaning |
|---|---|
| `est_audience` | Lemmy: subscribers. Discourse: `users_count` from about.json. PeerTube: `totalUsers` from the joinpeertube index. Blank if unknown (never estimated). |
| `self_promo_allowed` | `yes` / `no` / `restricted` (e.g. only in a given channel, or only after asking) / blank = unknown |
| `disclosure_required` | `yes` (AI use must be disclosed or tagged) / `prohibited` (AI-generated content banned) / blank = unknown |
| `automation_allowed` | `yes` (public API meant for automated use) / `no` / `approval` (only mod-approved bots) / `restricted` / blank = unknown |
| `rules_quote` | A verbatim quote of 25 words or fewer from `rules_url`, or from the API source named in `notes` |
| `discovered_via` | The search or API URL that surfaced the row, or "(direct probe of known site)" |

Fields we could not support from a fetched page are left blank. Nothing was guessed.

## Counts

By `connector_key`:

| connector_key | rows |
|---|---|
| lemmy | 44 |
| assisted | 44 |
| discourse | 40 |
| devto | 15 |
| peertube | 13 |
| slack | 10 |
| mrss | 4 |
| webhook | 4 |
| ghost | 1 |
| **total** | **175** |

- **Per host:** at most 15 rows per host (lemmy.world 15, dev.to 15, lemmy.ml 13, programming.dev 5).
- **By category:** ai 53, creators 28, founders 28, video 22, saas 21, tech 13, marketing 10.
- **Rules coverage:**
  - 160 of 175 rows have a `rules_url`, and every one returned 200.
  - 106 rows have a verified quote.
  - self_promo known for 47 rows, disclosure for 26, automation for 50.

## Limitations and caveats

- **Discourse rules pages.**
  - **`/guidelines` was not fetched.** Discourse's default robots.txt has `Disallow: /g`, which prefix-matches
    `/guidelines`. We used `/tos` instead, or the page `/tos` redirects to.
  - **Generic terms, not community rules.** Many forums redirect `/tos` to the vendor's general terms. For those
    rows the rules are generic, as the notes say.
  - **Two default ToS templates.** Most forums use one of two default Discourse ToS texts:
    - "no advertisements/solicitations; no automated access"
    - "no spam / machine-generated content"
- **Excluded hosts.**
  - **robots.txt:** Hacker Noon, lobste.rs, Changelog's submit page, Slashdot's submission page, sopuli.xyz,
    tube.tchncs.de and others disallowed our User-Agent, so they are not in the catalogue.
  - **Cloudflare 403 or 429:** AlternativeTo, There's An AI For That, Toolify, Medium publications and StackShare.
- **Dropped in the final pass:** lemmy.dbzer0.com (2 communities). It served an anti-bot page in place of API JSON.
- **lemmy.ca:** the `/c/Marketing` HTML page returned an anti-bot challenge with HTTP 200. The community was
  verified through its JSON API, as the row notes say.
- **Rule summaries were read during discovery.** The summaries (`link_policy`, `frequency_cap`, the
  self-promo/disclosure/automation flags) and the "instance rules" notes were written from pages read during
  discovery on 2026-10-14/15. The final pass re-verified quotes and reachability, not every summary.
- **Coverage gaps.**
  - **Ghost:** only one row, The Gradient. Few Ghost publications invite outside submissions, and native Ghost
    publishing needs the publication's Admin API key, so Ghost destinations are mainly owned or partner
    publications.
  - **webhook/partner:** only real public submission or ping APIs (IndexNow, the WebSub hubs, Ping-o-Matic).
    We found no newsletter with a public submission API.
- **Slack:** these are third-party workspaces. A bot needs admin approval, so `automation_allowed=no` and they should
  be treated as assisted (L1) unless the admins install the app.
- **Audience numbers** are platform counts at fetch time: registered users or subscribers, not active readers.
  Active-user counts are in `notes` where the API gives them.
- **Paid listings:** some directories charge for a listing (Easy With AI, TopAI.tools, paid fast-tracks). This is
  stated in `notes`. Using them is a business decision; the software stack stays free.
- **No relevance score yet.** Some rows are adjacent rather than core (e.g. freeCodeCamp, Echo JS, Hackaday) and
  should score lower once scored (seed_v2).

## seed_v2

`seed_v2.csv` is generated from `seed_v1.csv` (which stays unchanged) by `uv run python scripts/p5c_catalogue.py seed-v2`.
It moves the 10 Slack rows to `assisted` (D8), gives the 4 MRSS rows our own `instance_feed_key`, and keeps only
structured rule values (free text moves to `rules_text`, unconfirmed). See `seed_v2_CHANGELOG.md`. The real import
leaves the rule columns out (`p5c_catalogue.py import-csv`), so that rules are only ever confirmed by a person (D6).
Results: `../real_catalogue_v1.csv` and `../reports/real_catalogue_report.md`.
