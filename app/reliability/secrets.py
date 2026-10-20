"""Credentials at rest: Fernet-encrypted JSON in ``connections.secret_enc`` (plan §4.3).

Every decrypted value is added to the ``SecretRegistry`` at once, so the log redactor scrubs it
from then on, wherever it shows up.
"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from app.errors import ErrorCode, IbxError
from app.reliability.redact import secret_registry


def encrypt_credentials(key: bytes, credentials: dict[str, str]) -> bytes:
    secret_registry.add_many(credentials.values())
    return Fernet(key).encrypt(json.dumps(credentials, sort_keys=True).encode())


def decrypt_credentials(key: bytes, token: bytes | None) -> dict[str, str]:
    if not token:
        return {}
    try:
        data = json.loads(Fernet(key).decrypt(token))
    except (InvalidToken, ValueError) as exc:
        raise IbxError(
            ErrorCode.CONNECTION_AUTH_EXPIRED,
            "Stored credentials can not be decrypted (SECRETS_KEY changed?); reconnect the account.",
            details={"reason": "decrypt_failed"},
        ) from exc
    credentials = {str(k): str(v) for k, v in data.items()}
    secret_registry.add_many(credentials.values())
    return credentials
