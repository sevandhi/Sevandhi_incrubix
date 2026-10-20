# Posting rules, spec checks and tailoring

This page lets you predict what the engine will say about a post **before** you run it.
Code: `app/rules/` (engine, checks, spec) and `app/tailoring/`. All of it is pure: no database, no network, no clock.

```python
from app.rules import evaluate, check_spec, RuleSet
from app.tailoring import tailor

decision = evaluate(rendition, ruleset, history, now, owner_domains=["creator.dev", "youtube.com/@creator"])
violations = check_spec(rendition, manifest_surface, destination.spec_overrides)
tailored = tailor(rendition, destination, manifest_surface, ruleset)   # .rendition .diff .warnings .violations
```

## 1. How a decision is made

1. `ruleset is None` gives **block `DEST_RULES_UNCONFIRMED`** at once. Nobody has confirmed this destination's rules.
   Rules that cannot be read (a misspelt key such as `disclosure_requried`, or `frequency_cap: often`) are **refused**
   with a validation error that suggests the right key. Through `resolve_ruleset()` this becomes 422
   `REQUEST_VALIDATION_FAILED`. A typo never switches a rule off.
2. Otherwise **every** check runs in this fixed order. Each check is one file in `app/rules/checks/`:

   | # | Check | Fails when | Outcome | Code |
   |---|---|---|---|---|
   | 1 | `automation` | `automation_allowed: false` | block | `DEST_RULE_AUTOMATION_NOT_ALLOWED` |
   | 2 | `language` | policy **must_match** (stated, or implied, see §6) and the content language is not one of `languages`. With policy **any**, a mismatch is only a `LANGUAGE_MISMATCH` warning | block | `DEST_RULE_LANGUAGE` |
   | 3 | `blocked_terms` | a blocked term appears as a whole word (case-insensitive) in the title, body, tags or link_url. For CJK/Thai terms, any substring match counts | block | `DEST_RULE_BLOCKED_TERM` |
   | 4 | `self_promo` | `disallowed` and the post is promotional (section 3); or `ratio(n)` and promotional posts would exceed 1 in n | block | `DEST_RULE_SELF_PROMO` |
   | 5 | `links` | `none`: any link. `no_shorteners`: a shortener link. `allowlist`: a link outside the list | block | `DEST_RULE_LINK_POLICY` |
   | 6 | `disclosure` | `required`, `ai_generated` is **true or null**, and no known disclosure line is present | **fix** (insert the line) or block if `auto_insert: false` | `DEST_RULE_DISCLOSURE` (block only) |
   | 7 | `frequency_cap` | at least `max_posts` counted posts are in the rolling window | reschedule | `DEST_RULE_FREQUENCY_CAP` |

3. **The action is the most serious outcome:** block > reschedule > allow_with_changes (a fix is needed) > allow.
4. **`code`** comes from the first check, in the order above, that produced that action. The error **`message`** is that same check's reason (so a capped post on a disclosure destination says "cap reached", not "disclosure required"). **`reasons`** lists every failing check, in order. **`fixes`** is always filled in, even when the post is blocked or rescheduled.
5. Because block beats reschedule and the cap is checked last, **a post that will be blocked is never rescheduled.** `retry_at` is set only when the action is `reschedule`.
6. `Decision.to_error()` gives the contract error: 409 for every `DEST_RULE_*` code. Only `DEST_RULE_FREQUENCY_CAP` is retryable, and only it carries `retry_at`. `details.evidence` names what fired.

**Warnings that never change the action:** `AI_STATUS_UNKNOWN`, `AI_DISCLOSURE_REQUIRED`, `DISCLOSURE_LANG_FALLBACK`, `LANGUAGE_MISMATCH` (policy `any`), `LANGUAGE_UNKNOWN` (implied must_match, language undetermined), `SELF_PROMO_RATIO`, `RULES_DEFAULTED`.

## 2. Frequency cap arithmetic

- **Window:** `(now - window, now]`. A post exactly one window old has left it.
- **What counts:** every history event except `failed / error / errored / rejected / blocked / cancelled / rescheduled / skipped`.
  - Deleted posts **count**, and so do timed-out ones (they may have gone out).
  - Reserved slots (`publishing`) count.
  - Future `scheduled` posts count only if they are less than one window ahead.
- **`retry_at`:** with k counted posts sorted oldest first, the slot opens when post number `k - max_posts + 1` leaves the window, so `retry_at = t[k - max_posts] + window` (0-based). In the usual case (k = max_posts) this is **`oldest_in_window + window`**.
- **Window format:** ISO-8601 (`P7D`, `PT12H`, `P1W`) or `1/week`. `P1M` is read as 30 days and `P1Y` as 365 days.
- **History scope:** `history` must contain only this destination's posts. The caller filters by `destination_key`.

Example: cap `2/P7D`, now `2026-10-20T09:00Z`, posts 1 and 3 days ago. The result is **reschedule**, with `retry_at` 2026-10-24T09:00Z.

## 3. When a post is promotional (`app/rules/promo_detector.py`)

The detector always runs, even when the caller sends `is_promotional: false`. Any one signal is enough:

| Signal | Example |
|---|---|
| `flag` | `is_promotional: true` |
| `own_link` | a link to an `owner_domains` entry or its sub-domain. An entry with a path (`youtube.com/@creator`) also matches on the path |
| `utm` | `?utm_source=…` |
| `affiliate` | `ref, aff, affiliate, coupon, promo, via, …` params, Amazon `?tag=`, `amzn.to` |
| `monetization_link` | patreon.com, ko-fi.com, buymeacoffee.com, gumroad.com, linktr.ee, … |
| `channel_link` | youtube.com/@x, /channel/, /c/, /user/; twitch.tv/x; tiktok.com/@x; instagram.com/x |
| `cta` | lexicon in en, es, fr, de, pt, hi (+ ja, ta, it, ru, zh): "subscribe", "use code", "link in bio", "suscríbete", "abonnez-vous", "rabattcode", "inscreva-se", "सब्सक्राइब", "チャンネル登録" …; and patterns such as "20% off" and "code SAVE20" |
| `self_reference` | "my channel / course / newsletter / app / product …", "our app …" |
| `shortener` | bit.ly, t.co, … (a shortener hides where it goes, possibly the creator's own site) |

Being conservative has a cost. Mentioning "subscribe" in a genuine question still blocks the post on a no-promotion destination, and the reason says why, so a person can reword it.

## 4. Links (`app/rules/links.py`)

A link is anything a reader could click:
- `link_url`
- `http(s)://…` in the title or body, in markdown, HTML or Slack form
- `www.…` addresses
- bare domains with a common TLD (`example.org/page`), because forums auto-link these

`canonical_url` (metadata) and media URLs (attachments) are **not** links. Domain matching is by host or sub-domain, so `github.com.evil.example` does **not** match `github.com`.

## 5. AI disclosure

- **Needed when:** `disclosure.required` and `ai_generated` is not `false`. Unknown counts as AI.
- **Already present when:** any known line appears in the title or body: a built-in line in any language, or one of the destination's `text_by_lang`.
- **Language:** the community's language (`rules.languages[0]`, filled in from `destination.language`), else the content language, else `en`. If no text exists for that language, English is used with a `DISCLOSURE_LANG_FALLBACK` warning. When the content is in a different language that also has a text, both lines are added, joined by ` / ` (German community, English post: `Hinweis: … / Disclosure: …`).
- **Texts:** `app/rules/disclosure.yaml` has en, es, de, fr, pt, hi, ja and ta.
- **Placement:** `top` or `bottom`, as its own paragraph (`<p>` for HTML).
- **`auto_insert: false`:** the destination wants the creator to write it, so the post is blocked with `DEST_RULE_DISCLOSURE`.

## 6. Language

- **Codes are normalised:** `en-US`, `EN`, `eng` and `English` all become `en`.
- **Content language:** `rendition.language` if set. Otherwise it is guessed from the script (Devanagari → hi, Tamil → ta, kana → ja, …) or from common words (en, de, es, fr, pt, it, nl). If the guess is not confident, the language is unknown.
- **Policy:** `language_policy` may be stated as `must_match` or `any`. **If it is not stated**, a community language that is not English implies `must_match`. A German community therefore blocks an English post by default; set `language_policy: any` to allow it.
- **Stated `must_match`** blocks on a mismatch and on an unknown content language. **Implied `must_match`** blocks only on a definite mismatch; an unknown language passes with `LANGUAGE_UNKNOWN`. **`any`** only warns (`LANGUAGE_MISMATCH`).
- **No translation:** the engine never translates. A German `must_match` community therefore blocks an English post (the brief's HT8 option b).

## 7. RuleSet input formats

Catalogue spelling is accepted, so one row is enough:

| Field | Accepted forms |
|---|---|
| frequency cap | `frequency_cap: 1/P7D` |
| self-promotion | `self_promo_allowed: false`, `self_promo: disallowed`, `ratio(10)` or `1:10` |
| link policy | `link_policy: allow\|any\|none\|no_shorteners`, `{allowlist: [...]}` or `allowlist:a.com,b.com` |
| disclosure | `disclosure_required: true` or `disclosure: {required, placement, text_by_lang, auto_insert}` |
| language | `language: de` or `languages: [de, en]`, with `language_policy: must_match` (also `strict`) or `any` |
| bookkeeping | `notes`, `confirmed_by`, `confirmed_at`, `version`, `evidence`, … are kept in `metadata`. Any other unknown key is an error |
| automation and terms | `automation_allowed`, `blocked_terms` |

A value that cannot be read (`self_promo: sometimes`, `frequency_cap: often`) raises an error. It never passes silently.

**Where the rules come from** (`app/rules/gate.py: resolve_ruleset(destination, catalogue_rules)`), highest precedence first:
1. inline `destination.rules`
2. the Appendix A destination-record fields on the destination (`frequency_cap`, `self_promo_allowed`, `link_policy`, `disclosure_required`, `automation_allowed`)
3. the confirmed catalogue rules

`destination.language` fills `languages`. The function returns `(RuleSet | None, source)`, e.g. `"inline+catalogue"`.

**Unstated rules are not enforced** (except the implied language policy above). `RuleSet.conservative_default()` is for "we know nothing": no automation (assisted only), 1 post per `P7D`, no self-promotion, no shorteners, disclosure required, `must_match`.

## 8. Spec checks (`DEST_SPEC_VIOLATION`, field-level)

`check_spec` returns **every** violation as `{field, rule, message, limit, actual}`. Keys:

| Key | What is checked |
|---|---|
| `title_max_chars`, `title_min_chars`, `title_required`, and the same three for `body` | text length |
| `count_unit` | how length is counted: `codepoints` (default; what Ruby, Rust and Postgres count), `graphemes`, `utf16` (JavaScript), `bytes` |
| `content_types` | the post type, and each media item's kind |
| `mime_types` | each media item's MIME type, with wildcards |
| `media_max_count`, `media_max_bytes`, `image_max_bytes`, `video_max_bytes`, `media_max_duration_s` | media limits |
| `tag_max`, `tag_pattern`, `tag_max_chars` | tags |
| `links_allowed: false` | no links |

- A missing MIME type is guessed from the file extension. If it is still unknown and types are restricted, that is a violation.
- An unknown size or duration is not guessed.
- Destination `spec_overrides` win over the manifest.

**In the contract `publish`**, call `apply_decision(rendition, decision)` after `evaluate`. Block or reschedule raises the 409; allow_with_changes returns the rendition with the disclosure inserted.

**Order in the contract `publish`** (contract pipeline): spec validation runs **before** the rules gate. A post that breaks both gets 422 `DEST_SPEC_VIOLATION` first.

## 9. Tailoring (`tailor`)

Steps, in order:
1. remove disallowed links
2. apply the profile
3. drop media the destination does not accept
4. fit tags
5. add the disclosure
6. fit the title, then the body
7. re-check the spec

Every change adds a warning and a `diff` entry. Anything still wrong after this is in `.violations` (`SPEC_UNRESOLVED`).

| Profile | Destinations | Changes |
|---|---|---|
| forum | discourse, lemmy | short non-question lines that start with a CTA are removed (`CTA_REMOVED`); other lines containing a CTA word are kept and flagged (`CTA_KEPT`); standalone link lines and `link_url` move to the bottom (link posts become text); a question is added at the end; the title becomes a question if it fits; Lemmy: hashtags and tags removed |
| article | devto, forem, ghost, peertube | `canonical_url` is set from `link_url` if missing. Dev.to: tags ASCII-folded, lowercased, alphanumeric only, at most 4 |
| slack | slack | markdown converted to mrkdwn: `*bold*`, `<url\|text>`, `<@U…>`, `&amp;` escaping; the title becomes the bold first line |
| newsletter | surface newsletter or email | the subject (title) is derived from the first line if missing, and fitted |
| mrss | mrss | description converted to plain text; title derived and fitted |
| generic | anything else | rules and spec fitting only |

**Truncation** (`truncate_words`):
- cuts at a word boundary, and never inside a grapheme cluster, URL, @mention, `!community@instance`, #tag, Slack token or markdown link
- for Chinese, Japanese and Thai, falls back to a character boundary
- the ellipsis is never glued to a URL

**Body fitting:**
- only the creator's text is shortened
- optional added lines (question, link, media link) are dropped first
- the disclosure is never dropped. If it cannot fit, the result is `DISCLOSURE_UNPLACEABLE` plus a `body/disclosure` violation

**Mentions:** Lemmy turns `@alice` into `@alice@<instance host>`. Discourse and Dev.to use `@alice`. The `mentions` list is rewritten the same way (`MENTIONS_ADAPTED`, or `MENTION_INSTANCE_UNKNOWN` if the instance host is unknown). Slack uses `<@U…>`. Dev.to symbol tags are spelled out: `c++` → `cpp`, `c#` → `csharp`, `.net` → `dotnet`.

Tailoring is deterministic (no LLM), so it does not make the content AI-generated. A future LLM rewrite must set `ai_generated: true`.

Warning codes:
- `LINK_REMOVED`, `CTA_REMOVED`, `CTA_KEPT`, `MENTIONS_ADAPTED`, `MENTION_INSTANCE_UNKNOWN`, `HASHTAGS_REMOVED`, `LINK_MOVED_TO_BOTTOM`
- `TITLE_QUESTION_ADDED`, `TITLE_NOT_QUESTION`, `TITLE_DERIVED`, `TITLE_TRUNCATED: 300→199 chars at word boundary`, `TITLE_FOLDED_INTO_TEXT`
- `CANONICAL_SET`, `TAGS_NORMALISED`, `TAGS_CHANGED`, `TAGS_DROPPED`
- `MEDIA_DROPPED: … linked instead`, `CONTENT_TYPE_CHANGED`
- `AI_DISCLOSURE_APPENDED`, `AI_DISCLOSURE_MISSING`, `AI_STATUS_UNKNOWN`, `DISCLOSURE_LANG_FALLBACK`, `DISCLOSURE_UNPLACEABLE`
- `BODY_TRUNCATED`, `BLOCK_DROPPED`
- `FORMAT_MRKDWN`, `FORMAT_PLAIN`
- `NO_RULES`, `SPEC_UNRESOLVED`

## 10. Worked prediction

**Input:**
- a Tamil title of 270 characters with emoji, and a 3 MB `image/webp`
- a Lemmy community: `content_types: [text, link]`, `title_max_chars: 200`
- rules: `disclosure_required: true`, `frequency_cap: 2/P7D`, with 2 posts 1 and 3 days ago
- `ai_generated` not set

**Predicted output:**

| Call | Result |
|---|---|
| `check_spec` | `title/title_max_chars` (limit 200, actual 270) and `media[0]/content_types` (actual `image/webp`) |
| `evaluate` | **reschedule** `DEST_RULE_FREQUENCY_CAP`, `retry_at` = (3 days ago) + 7 days; `fixes` = Tamil disclosure at the bottom; warnings `AI_STATUS_UNKNOWN`, `AI_DISCLOSURE_REQUIRED` |
| contract `publish` | 422 `DEST_SPEC_VIOLATION` (the spec runs first); no platform call |
| `tailor` | title cut to ≤200 at a word or grapheme boundary (`TITLE_TRUNCATED`); webp dropped and linked (`MEDIA_DROPPED`); Tamil disclosure appended. The cap still reschedules |

## 11. Add a rule

1. Add a red case to `tests/rules/rule_cases.yaml`.
2. Write `app/rules/checks/<rule>.py` with `NAME` and `check(ctx) -> CheckOutcome`.
3. Add it to `CHECKS` in `app/rules/checks/__init__.py`, in the right position.
4. Add the field to `RuleSet`.
5. Run `uv run pytest -q tests/rules` and `uv run python scripts/rule_safety.py`. The goal is 0 missed.
