"""Tests must not see settings leaked from the shell or the repo's ``.env``.

``test_pack_files_caption_deep_link_and_checklist`` failed in a clean-copy run (MH8) only: ``prove_mh8`` exports
``PUBLIC_BASE_URL=http://127.0.0.1:<port>`` for ``docker compose`` and then runs ``scripts/test.sh`` in the same
environment, so the test's settings got the stack's URL instead of the default. The same leak could switch
``LLM_PROVIDER`` to a real provider (the repo's ``.env`` may hold a real ``GEMINI_API_KEY``).

The outer test runs the inner one in a fresh pytest process with leaked values in its environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LEAKED = {"PUBLIC_BASE_URL": "http://127.0.0.1:43601", "LLM_PROVIDER": "auto", "GEMINI_API_KEY": "leaked-fake-key-000",
          "SIMULATION_MODE": "true", "APP_ENV": "demo"}


def test_inner_settings_never_see_the_leaked_environment(make_settings):
    if os.environ.get("IBX_P3C_INNER") != "1" and not os.environ.get("PUBLIC_BASE_URL"):
        pytest.skip("runs inside test_settings_are_isolated_from_the_shell_and_dotenv")
    from app.config import get_settings
    from app.providers.llm.config import LLMSettings, get_llm_settings

    settings = make_settings()
    assert settings.PUBLIC_BASE_URL == "http://127.0.0.1:8088"
    assert settings.SIMULATION_MODE is False and settings.APP_ENV == "test"
    llm = get_llm_settings()
    assert llm.LLM_PROVIDER == "mock" and llm.GEMINI_API_KEY is None
    assert LLMSettings().GEMINI_API_KEY is None  # neither the shell nor the repo's .env
    assert get_settings().PUBLIC_BASE_URL == "http://127.0.0.1:8088"


def test_settings_are_isolated_from_the_shell_and_dotenv():
    env = {**os.environ, **LEAKED, "IBX_P3C_INNER": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"{Path(__file__).relative_to(ROOT)}::test_inner_settings_never_see_the_leaked_environment"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0 and "1 passed" in result.stdout, result.stdout[-3000:] + result.stderr[-1000:]
