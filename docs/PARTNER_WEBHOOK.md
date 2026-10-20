# IncuBrix partner webhook protocol (version 2026-10)

A partner who implements this small HTTP API can receive posts from IncuBrix through the generic
`webhook` connector. No IncuBrix code needs to change: the partner is added as a destination with
`instance.endpoint_url` and a shared secret. A working reference partner is `mocks/platforms/routes/partner.py`.

## 1. Set-up

| Item | Value |
|---|---|
| Base URL | `endpoint_url`, for example `https://partner.example/incubrix` |
| Shared secret | a random string of 32 bytes or more, exchanged out of band; IncuBrix stores it encrypted (`connection.credentials.secret`) |
| Content type | `application/json; charset=utf-8` |
| Protocol header | IncuBrix sends `X-IncuBrix-Protocol: 2026-10` |

## 2. Signatures (every request)

Every request, including `GET` and `DELETE` (which have an empty body), carries:

```
X-IncuBrix-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + raw request body)>
```

The partner must:
1. Parse `t` and `v1`. If the header is missing or malformed, answer `401 {"error":"invalid_signature"}`.
2. Reject a timestamp more than **300 seconds** away from its own clock with
   `400 {"error":"timestamp_outside_tolerance"}`. This is the replay window.
3. Recompute the HMAC over the **exact raw body bytes** (do not re-serialise the JSON) and compare it in
   constant time. On a mismatch answer `401 {"error":"invalid_signature"}`.

Reference check (Python):
```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes) -> bool:
    fields = dict(p.split("=", 1) for p in header.split(","))
    t = int(fields["t"])
    if abs(time.time() - t) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, fields["v1"])
```

## 3. Endpoints

### `POST {endpoint_url}/posts`: create a post
Headers: the signature and `Idempotency-Key: <key>`. The key is stable for one publish request, so
IncuBrix sends the same key again when it retries after a timeout.

Body:
```json
{
  "event": "post.create",
  "idempotency_key": "ibx-abcdefghijkl",
  "rendition": {"title": "...", "body": "...", "body_format": "markdown", "content_type": "text",
                "canonical_url": null, "link_url": null, "media": [{"url": "...", "mime": "image/png"}],
                "tags": [], "mentions": [], "language": "en", "ai_generated": false},
  "destination": {"destination_id": "dest_...", "name": "...", "surface": "post"}
}
```

Answers:
- `201 {"id": "...", "url": "https://partner.example/p/123", "status": "published"}` for a new post.
- **Deduplication.** The same `Idempotency-Key` with the same body returns `200` and the **same** post
  (optionally with `Idempotent-Replayed: true`). The partner must never create a second post. Keep keys
  for at least 24 hours.
- The same key with a different body returns `409 {"error":"idempotency_conflict"}`.
- A missing `Idempotency-Key` returns `400 {"error":"idempotency_key_required"}`.

`status` is one of `published`, `pending` (in review), `scheduled`, `draft`, `removed` or `deleted`.

### `GET {endpoint_url}/posts?idempotency_key=<key>`: find a post by key
Returns `200 {"posts": [ {id, url, status}, ... ]}`, with an empty list when nothing matches. IncuBrix
uses this after a timeout to find a post that was created but never confirmed.

### `GET {endpoint_url}/posts/{id}`: status
Returns `200 {id, url, status}`. Answer `404` or `410` once the post is gone.

### `DELETE {endpoint_url}/posts/{id}`: delete
Returns `204` (or `200`). A second delete may answer `404`; IncuBrix treats that as already deleted.
After a delete, the public `url` must answer `404` or `410`. IncuBrix certification checks this.

## 4. Errors

Use a JSON body `{"error": "<token>", "message": "<human text>"}`. IncuBrix maps the answers like this:

| Partner answer | IncuBrix error | Effect |
|---|---|---|
| `401` (`invalid_signature`, `revoked`) or `403` | `CONNECTION_AUTH_EXPIRED` | The connection is paused until the owner reconnects; no retries |
| `400 timestamp_outside_tolerance` | `PLATFORM_UNAVAILABLE` | Retried with a fresh signature; repeated failures open the circuit |
| `400` (other), `409`, `413`, `422` | `PLATFORM_CONTENT_REJECTED` | Not retried; your `message` is shown to the user |
| `429` + `Retry-After` (seconds or HTTP-date) or `X-RateLimit-Reset` (epoch seconds) | `PLATFORM_RATE_LIMIT` | No calls until the reset time |
| `5xx`, timeout | `PLATFORM_UNAVAILABLE` | Retried with back-off; 5 failures in a row open the circuit for 5 minutes |

To **revoke** IncuBrix's access, answer `401 {"error":"revoked"}` to every request. Rotating the secret
has the same effect until the new secret is entered in IncuBrix.

## 5. Checklist for partners
- [ ] Verify the signature over the raw body, enforce the 300-second window, and compare in constant time.
- [ ] Deduplicate `POST /posts` by `Idempotency-Key` (same body → same post; different body → 409).
- [ ] Implement `GET /posts?idempotency_key=`, `GET /posts/{id}` and `DELETE /posts/{id}`.
- [ ] Make the public URL answer `404`/`410` after a delete.
- [ ] Send `Retry-After` or `X-RateLimit-Reset` with every `429`.
