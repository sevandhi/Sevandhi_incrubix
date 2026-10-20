# Rules research v1 (OUT4): AI-assisted, disclosed

`rules_research_v1.csv` holds one row per destination in `catalogue/seeds/seed_v3.csv` (277 rows) with the
destination's **researched rules**. `pages/<page_sha256>.txt` holds the plain text each quote was verified against.
`scripts/prove_out4_research.py` recounts everything offline from these files.

**Disclosure.** The rules were researched with AI assistance: an AI agent
fetched each rules page, read the extracted text and chose each value and its quote. A script then verified every quote
verbatim against the saved text. **No person has confirmed these rules yet.** Every row is `rules_status = proposed`
and must be confirmed by a person before anything is posted (decision D6). Nothing was posted anywhere.

## Result

`uv run python scripts/prove_out4_research.py --require-met` gives **176 counted destinations** (target 165, brief 150):

| connector_key | rows | researched | counted |
|---|---|---|---|
| discourse | 107 | 107 | 90 |
| lemmy | 62 | 62 | 53 |
| devto | 15 | 15 | 15 |
| assisted (incl. 8 Mastodon servers, 2 Slack, 2 HN) | 71 | 57 | 15 |
| mrss | 4 | 3 | 2 |
| peertube | 13 | 12 | 1 |
| ghost | 1 | 1 | 0 |
| webhook | 4 | 4 | 0 |
| **total** | **277** | **261** | **176** |

83 counted rows are seed_v2 rows; 93 are rows added in seed_v3 (see `catalogue/seeds/seed_v3_CHANGELOG.md`).
At most 15 counted rows share a host (lemmy.world, dev.to and lemmy.ml each have 15 rows).

Per field, over the 261 researched rows (quote-backed / `not_stated`; 0 quotes failed):

| field | quote-backed | not_stated | values |
|---|---|---|---|
| self_promo_allowed | 188 | 73 | no 85, conditional 67, yes 36 |
| automation_allowed | 107 | 154 | no 64, approval 32, yes 11 |
| ai_content_banned | 89 | 172 | yes 50, no 35, restricted 4 |
| disclosure_required | 77 | 184 | yes 76, no 1 |
| link_policy | 31 | 230 | restricted 28, any 2, no_shorteners 1 |
| frequency_cap | 4 | 257 | 10/P1D, 1/P1W, 2/P1W, 24/P1D |

## What a counted row means

A row counts only if **all** of these hold (re-derived by the prove script, not taken from the `counted` column):

1. The destination URL answered **HTTP 200** at check time (`url_http_status`, `url_fetched_at`).
2. Every rules source the row uses (up to 3: `rules_source_url`, `_2`, `_3`) answered **200** at check time. Its
   extracted text is saved as `pages/<page_sha256>.txt`, and `sha256(text) == text_sha256`.
3. **At least 2 of the 6 fields** have a value backed by a quote of **≤ 25 words**. The quote must appear verbatim in
   the text of the source named in `<field>_quote_source`. Matching collapses whitespace and folds curly
   quotes/dashes to ASCII, and nothing else. Every other field is exactly `not_stated`: the sources were read and do not say.
4. `relevance_score` is 3 to 5 with a reason, and `connector_key` is set.
5. The host is not one of the 15 major platforms, and at most 15 rows per host count.

`page_sha256` is the sha256 of the bytes received. `text_sha256` is the sha256 of the saved extracted text.

## Value vocabulary

| field | values |
|---|---|
| `frequency_cap` | `N/<ISO-8601 period>` (e.g. `10/P1D`) |
| `self_promo_allowed` | `yes` (sharing your own work is invited), `no` (advertising or self-promotion banned), `conditional` (allowed under stated limits: a channel, a share, relevance, permission) |
| `link_policy` | `any`, `none`, `no_shorteners`, `allowlist`, `restricted` (some links or domains are banned, or a link form is required) |
| `disclosure_required` | `yes` when AI use, affiliation or automated (bot) accounts must be disclosed or labelled; `no` when the source says disclosure is optional |
| `automation_allowed` | `yes` (allowed, possibly with a bot flag), `no`, `approval` (only with mod or admin approval) |
| `ai_content_banned` | `yes`, `no` (allowed, possibly with disclosure), `restricted` (for example, accounts may not post only AI content) |

The words `conditional`, `restricted` and `approval` are research values. The rules engine needs structured values,
so the person confirming a row picks the engine value (the conservative choice is "not allowed").

## Method

1. **Sources per row.** Each row has its own rules page, plus the platform's machine-readable rule text:
   - Lemmy: the community sidebar via `/api/v3/community?name=…`, plus the instance rules (site sidebar `/api/v3/site`,
     or the instance's linked legal page: legal.lemmy.world ToS, the Lemmy Code of Conduct for lemmy.ml, the
     programming.dev Automation Guidelines, the lemmy.zip Code of Conduct).
   - Discourse: `/tos`, pinned guideline topics as `/t/<slug>/<id>.json`, and category descriptions in
     `/categories.json`. The `/guidelines` page is robots-disallowed on Discourse (`Disallow: /g`), so it was not
     fetched.
   - PeerTube: `/api/v1/config/about`.
   - Mastodon: `/api/v1/instance`, which holds the server rules.
   - dev.to: the AI-assisted-articles guidelines and the DEV terms.
   - Others: the site's own guidelines, FAQ or terms page.

   Instance-wide or platform-wide rules apply to every community on that instance. The `notes` column says when a
   value comes from them.
2. **Fetching.** A read-only research fetcher (scratch, not shipped) used Python + httpx:
   - GET only, User-Agent `IncuBrix-assessment-catalogue-research/0.2 (read-only; contact: candidate)`.
   - Redirects were followed by hand, and robots.txt was checked on **every hop**. The RFC 9309 matcher handles
     `*`/`$`, the longest match wins and Allow wins ties. A robots.txt 4xx means allow; a 5xx or network error means
     disallow.
   - Requests to the same host were serialised by a lock, ≥ 1.1 s apart (robots.txt fetches included). The 1,524
     requests ran on 2026-10-20 from about 06:59 to 07:23 UTC.
3. **Text extraction.** This step was deterministic:
   - HTML: script, style, svg and head elements were dropped; block tags became newlines.
   - JSON APIs: only the human-readable rule fields were kept (sidebar, terms, code of conduct, rules, description).
   - The text was saved as UTF-8 with `\n` line endings. No HTML, scripts or images are stored.
4. **Reading and deciding.** The agent read each source (first a keyword-filtered view, then the full text around each
   rule) and recorded, per field, a value plus a quote, or nothing. Fields left out became `not_stated`.
5. **Verification.** The build script re-checked every quote against the saved text (0 failures). The prove script
   repeats this check independently.
6. **Relevance.**
   - seed_v2 rows keep the catalogue-import relevance score from `real_catalogue_v1.csv`.
   - New rows, and 9 seed_v2 rows that were never imported, are scored by the same deterministic scorer
     `app.catalogue.relevance.lexicon_score`. Its inputs are the name, the description, and a category and tags the
     agent chose. Those tags are pending human review (D11).

## Limitations (read before relying on this)

- **AI-researched, unconfirmed.** Values are one reader's interpretation of the quoted text. Some mappings are
  judgement calls, each noted in `notes`:
  - Bot-flag rules are recorded as `disclosure_required=yes`.
  - "No advertising" is recorded as `self_promo_allowed=no`.
  - A forum's generic terms (the standard Discourse template "You may not send advertisements…" / "You may not
    automate access to the forum…") give `self_promo_allowed=no` and `automation_allowed=no` even where a showcase
    category invites sharing. In that case the category's own invitation is used for that category row.
  - The WordPress-derived terms clause "not machine- or randomly-generated" is recorded literally as
    `ai_content_banned=yes`. It was written against spam bots, so a person should confirm it.
  - Please spot-check at least 20 rows (D6).
- **Template terms.** 90 counted rows are Discourse forums or categories, and many rely on the forum's generic `/tos`
  template rather than community-written guidelines. They are real terms that govern posting there, but they are
  thin.
- **Categories as destinations.** 43 rows are Discourse categories (showcase, share or tutorial) of forums that are
  also rows. They are distinct posting destinations with their own description, but they share the forum's terms.
- **Mastodon servers** are `assisted` (a person posts from an account there; no connector). General-purpose servers
  score 1 to 2 for relevance and do not count.
- **Not fetched (robots or blocks):** 31 hosts were never fetched beyond robots.txt.
  - In 24 of them the robots.txt request itself failed (network error or timeout). Following RFC 9309, that was
    treated as "disallow".
  - The rest disallowed our user agent.
  - The hosts include cuddly.tube, peakfreelance.com, 20 probed Discourse forums, 8 Lemmy instances, scholar.social
    and pixelfed.social.
  - lemmy.zip answered HTTP 429 seventeen times during the community search. Those searches were retried later at an
    8 s gap.
  - lemmings.world's `/api/v3/site` redirected to an unrelated casino page and was not used.
- **Thin rules.** These rows were researched but do not count, because their pages state fewer than 2 rule fields:
  - most launch sites and directories;
  - most PeerTube instances (12 researched, 1 counted);
  - the webhook/ping APIs and The Gradient.
- **Page text is copied from public pages** only so the quotes can be verified again. The total is 2.4 MB of plain
  text. Remove `pages/` if redistribution is a concern; the prove script then reports the rows as unverifiable.
- **Interruption.** The research was interrupted once by a usage limit, which wiped the scratch directory. About 12
  early fetches were lost, and everything was fetched again; all timestamps come from the final run.
- **Point in time.** Pages change. `fetched_at` and `page_sha256` record what was read and when.
