"""Checks for the recorded golden fixtures and the bootstrap helpers. No Docker needed."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import jwt
import pytest

HERE = Path(__file__).parent
REPO = HERE.parents[2]
FIXTURES = sorted(p for p in HERE.glob("*/*.json"))


def _load_ibxboot():
    spec = importlib.util.spec_from_file_location("ibx_p2a_ibxboot", REPO / "deploy" / "bootstrap" / "ibxboot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


ibxboot = _load_ibxboot()


def fx(name: str) -> dict:
    return json.loads((HERE / f"{name}.json").read_text())


def test_fixtures_exist_for_lemmy_and_ghost():
    names = {f"{p.parent.name}/{p.stem}" for p in FIXTURES}
    for required in (
        "lemmy/create_post_ok", "lemmy/get_post_ok", "lemmy/purge_post", "lemmy/create_post_title_too_long",
        "lemmy/create_post_invalid_token", "lemmy/create_post_rate_limited", "lemmy/get_post_not_found",
        "ghost/create_post_ok", "ghost/get_post_ok", "ghost/delete_post", "ghost/create_post_title_too_long",
        "ghost/expired_token", "ghost/invalid_token_bad_signature", "ghost/get_post_not_found",
    ):
        assert required in names


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_fixture_shape_and_no_secrets(path: Path):
    raw = path.read_text()
    data = json.loads(raw)
    assert data["fixture"] == f"{path.parent.name}/{path.stem}"
    assert data["platform"] == path.parent.name
    assert isinstance(data["response"]["status"], int)
    assert data["request"]["method"] in {"GET", "POST", "PUT", "DELETE"}
    assert data["recorded_at"].endswith("Z")
    # no live tokens, admin keys or bearer values may survive redaction
    assert not ibxboot.JWT_RE.search(raw)
    assert not ibxboot.GHOST_ADMIN_KEY_RE.search(raw)
    assert not re.search(r"(Bearer|Ghost) (?!\*\*\*REDACTED)", data["request"]["headers"].get("Authorization", "Bearer ***REDACTED***"))
    api_key = data["request"]["headers"].get("Api-Key")
    assert api_key in (None, "***REDACTED***", "0" * 64)  # the all-zero key is the deliberate invalid-key probe


def test_lemmy_error_facts_recorded():
    rl = fx("lemmy/create_post_rate_limited")["response"]
    assert rl["status"] == 400 and rl["json"] == {"error": "rate_limit_error"}
    assert not {"retry-after", "x-ratelimit-reset", "ratelimit-reset"} & set(rl["headers"])
    assert fx("lemmy/create_post_title_too_long")["response"]["json"]["error"] == "invalid_post_title"
    assert fx("lemmy/get_post_not_found")["response"]["json"]["error"] == "couldnt_find_post"
    bad = fx("lemmy/create_post_invalid_token")["response"]
    assert (bad["status"], bad["json"]["error"]) == (401, "incorrect_login")
    cfg = fx("lemmy/site_rate_limit_config")["response"]["json"]["site_view"]["local_site_rate_limit"]
    assert (cfg["post"], cfg["post_per_second"]) == (2, 600)


def test_ghost_error_facts_recorded():
    assert fx("ghost/create_post_title_too_long")["response"]["status"] == 422
    exp = fx("ghost/expired_token")["response"]
    assert exp["status"] == 401 and exp["json"]["errors"][0]["code"] == "INVALID_JWT"
    assert fx("ghost/delete_post")["response"]["status"] == 204
    assert fx("ghost/get_post_not_found_after_delete")["response"]["status"] == 404


def test_ghost_admin_jwt_is_valid_hs256():
    key_id, secret = "a" * 24, "0f" * 32
    token = ibxboot.ghost_admin_jwt(f"{key_id}:{secret}", ttl_s=300)
    assert jwt.get_unverified_header(token)["kid"] == key_id
    claims = jwt.decode(token, bytes.fromhex(secret), algorithms=["HS256"], audience="/admin/")
    assert 0 < claims["exp"] - claims["iat"] <= 300
    assert abs(claims["iat"] - time.time()) < 5
    with pytest.raises(ValueError):
        ibxboot.split_ghost_admin_key("no-colon")


def test_redactor_masks_values_keys_and_patterns():
    r = ibxboot.Redactor(["supersecretvalue"])
    out = r.data({
        "jwt": "abc.def.ghi",
        "note": "token supersecretvalue inside",
        "auth": "Bearer abcdefghijklmnop",
        "k": "0123456789abcdef01234567:" + "ab" * 32,
        "error": "incorrect_login",
        "nested": [{"password": "x" * 12}],
    })
    assert out["jwt"] == ibxboot.REDACTED
    assert "supersecretvalue" not in out["note"]
    assert out["auth"] == f"Bearer {ibxboot.REDACTED}"
    assert out["k"] == ibxboot.REDACTED
    assert out["error"] == "incorrect_login"
    assert out["nested"][0]["password"] == ibxboot.REDACTED
