"""Two small helpers for the Lemmy connector.

``local_credentials``: in the default "local" compose mode our OWN Lemmy container runs next to the API,
and ``deploy/bootstrap`` writes dev-only credentials for it to ``$BOOTSTRAP_OUT_DIR/lemmy.json``. When a
call carries no credentials at all and targets exactly that instance (directly, or through its toxiproxy),
those credentials are used. Any other host gets nothing, so this can never post to a real community.

``aux_call``: simulated faults (``X-Sim-Fault``) are meant for the platform call a method is about (the
create, the delete...). Helper lookups made on the way (community id, login, rate-limit config) are run
with the fault switched off, so ``timeout_after_create`` really hits the create request.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from app.config import Settings
from app.reliability.redact import secret_registry


def _host(url: str) -> str:
    parts = urlsplit(url if "://" in url else f"http://{url}")
    return f"{(parts.hostname or '').lower()}:{parts.port or (443 if parts.scheme == 'https' else 80)}"


def local_credentials(settings: Settings, api_base: str) -> dict[str, str]:
    """Bootstrap credentials for our own local Lemmy, or ``{}`` if ``api_base`` is any other host."""
    path = Path(settings.BOOTSTRAP_OUT_DIR) / "lemmy.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    own_hosts = {_host(data.get("base_url") or ""), _host(settings.IBX_LEMMY_PROXY_URL)}
    if _host(api_base) not in own_hosts:
        return {}
    credentials = {
        "jwt": data.get("jwt") or "",
        "username": data.get("admin_username") or "",
        "password": data.get("admin_password") or "",
    }
    credentials = {name: value for name, value in credentials.items() if value}
    secret_registry.add_many(credentials.values())  # redact them from every log line
    return credentials


@contextmanager
def aux_call() -> Iterator[None]:
    """Run a helper request without the caller's simulated fault (sim/dev only; a no-op otherwise)."""
    from app.contract.router import SIM_FAULT  # imported here: the router module imports the connectors' base

    token = SIM_FAULT.set(None)
    try:
        yield
    finally:
        SIM_FAULT.reset(token)
