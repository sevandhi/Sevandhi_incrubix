"""Engagement by the instance ADMIN on OUR OWN local instances (OUT2 evidence). Runs INSIDE the api container
(it reads the bootstrap's dev-only credentials from /bootstrap/out and never prints them):

    docker compose exec -T api python - <action> '<json args>' < scripts/p5a_instance_actions.py

Actions (each is one legitimate action a community admin can take on their own instance; no extra accounts):
    lemmy_comment      {"post_id"}      the admin comments on the post (Lemmy's own counter: counts.comments)
    lemmy_sidebar      {"text", "community"}   the admin edits a test community's sidebar (rules drift demo)
    peertube_engage    {"video_id"}     root likes the video, comments once, and reports one playback view
    discourse_reply    {"topic_id"}     the admin replies once in the topic
    discourse_click    {"topic_id", "post_id", "url"}   the admin follows the link in the post via /clicks/track
Output: one JSON object (statuses and ids only).
"""

from __future__ import annotations

import json
import sys

import httpx

OUT = "/bootstrap/out"


def _load(name: str) -> dict:
    with open(f"{OUT}/{name}") as fh:
        return json.load(fh)


def lemmy_comment(args: dict) -> dict:
    info = _load("lemmy.json")
    r = httpx.post(f"{info['base_url']}/api/v3/comment", headers={"Authorization": f"Bearer {info['jwt']}"},
                   json={"post_id": int(args["post_id"]), "content": "Thanks, admin comment on our own test instance (OUT2)."},
                   timeout=15)
    return {"action": "lemmy_comment", "http_status": r.status_code, "comment_id": (r.json().get("comment_view") or {}).get("comment", {}).get("id")}


def lemmy_sidebar(args: dict) -> dict:
    info = _load("lemmy.json")
    auth = {"Authorization": f"Bearer {info['jwt']}"}
    community_id = info["communities"][args.get("community", "ibx_test")]
    r = httpx.put(f"{info['base_url']}/api/v3/community", headers=auth,
                  json={"community_id": community_id, "description": args["text"]}, timeout=15)
    return {"action": "lemmy_sidebar", "http_status": r.status_code}


def _peertube_token(base: str, password: str) -> str:
    client = httpx.get(f"{base}/api/v1/oauth-clients/local", timeout=15).json()
    token = httpx.post(f"{base}/api/v1/users/token", timeout=15, data={
        "client_id": client["client_id"], "client_secret": client["client_secret"], "grant_type": "password",
        "username": "root", "password": password})
    token.raise_for_status()
    return token.json()["access_token"]


def peertube_engage(args: dict) -> dict:
    base = args.get("base_url", "http://peertube.ibx.test:9000")
    auth = {"Authorization": f"Bearer {_peertube_token(base, args['password'])}"}
    vid = args["video_id"]
    rate = httpx.put(f"{base}/api/v1/videos/{vid}/rate", headers=auth, json={"rating": "like"}, timeout=15)
    comment = httpx.post(f"{base}/api/v1/videos/{vid}/comment-threads", headers=auth, timeout=15,
                         json={"text": "Admin comment on our own test instance (OUT2)."})
    view = httpx.post(f"{base}/api/v1/videos/{vid}/views", headers=auth, json={"currentTime": 3}, timeout=15)
    return {"action": "peertube_engage", "rate_status": rate.status_code, "comment_status": comment.status_code,
            "view_status": view.status_code}


def _discourse() -> tuple[str, dict]:
    info = _load("discourse.json")
    return info.get("base_url", "http://discourse").rstrip("/"), {"Api-Key": info["api_key"], "Api-Username": info["api_username"]}


def discourse_reply(args: dict) -> dict:
    base, headers = _discourse()
    r = httpx.post(f"{base}/posts.json", headers=headers, timeout=20,
                   json={"topic_id": int(args["topic_id"]), "raw": "Admin reply on our own test forum (OUT2 evidence)."})
    return {"action": "discourse_reply", "http_status": r.status_code}


def discourse_click(args: dict) -> dict:
    base, headers = _discourse()
    r = httpx.post(f"{base}/clicks/track", headers=headers, timeout=20,
                   data={"url": args["url"], "post_id": int(args["post_id"]), "topic_id": int(args["topic_id"])})
    return {"action": "discourse_click", "http_status": r.status_code}


ACTIONS = {f.__name__: f for f in (lemmy_comment, lemmy_sidebar, peertube_engage, discourse_reply, discourse_click)}

if __name__ == "__main__":
    name, raw = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "{}"
    try:
        result = ACTIONS[name](json.loads(raw))
    except Exception as exc:  # noqa: BLE001 - report the failure class only (never a credential)
        result = {"action": name, "error": type(exc).__name__}
    sys.stdout.write(json.dumps(result) + "\n")
