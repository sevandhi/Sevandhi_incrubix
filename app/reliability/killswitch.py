"""Kill switch (plan §4.3): a connector is disabled by env (``DISABLED_CONNECTORS``) or at runtime
(``PUT /api/v1/connectors/{key}/kill-switch``, stored in ``connector_flags``). Checked first in every method.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.tables import ConnectorFlagRow
from app.errors import ErrorCode, IbxError


async def disabled_reason(session: AsyncSession | None, settings: Settings, connector_key: str) -> str | None:
    """Why the connector is disabled, or ``None`` if it is enabled."""
    if connector_key.lower() in settings.DISABLED_CONNECTORS:
        return "disabled by DISABLED_CONNECTORS"
    if session is None:
        return None
    row = await session.get(ConnectorFlagRow, connector_key)
    if row is not None and row.disabled:
        return row.reason or "disabled at runtime"
    return None


async def ensure_enabled(session: AsyncSession | None, settings: Settings, connector_key: str) -> None:
    reason = await disabled_reason(session, settings, connector_key)
    if reason is not None:
        raise IbxError(
            ErrorCode.CONNECTOR_DISABLED,
            f"Connector '{connector_key}' is disabled ({reason}).",
            details={"connector_key": connector_key, "reason": reason},
        )


async def set_flag(session: AsyncSession, connector_key: str, disabled: bool, reason: str | None) -> None:
    stmt = insert(ConnectorFlagRow).values(connector_key=connector_key, disabled=disabled, reason=reason)
    stmt = stmt.on_conflict_do_update(
        index_elements=[ConnectorFlagRow.connector_key], set_={"disabled": disabled, "reason": reason}
    )
    await session.execute(stmt)
