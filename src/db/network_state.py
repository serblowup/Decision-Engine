from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import asyncpg

from src.config import settings
from src.models.network import Device, NetworkState, VlanAssignment, VlanInfo

if TYPE_CHECKING:
    from src.topology.graph import Topology

logger = logging.getLogger(__name__)


async def _fetch_rows(
    conn_or_pool: asyncpg.Connection[Any] | asyncpg.Pool,
    query: str,
    *params: Any,
) -> list[asyncpg.Record]:
    if isinstance(conn_or_pool, asyncpg.Pool):
        async with conn_or_pool.acquire() as conn:
            return await conn.fetch(query, *params)
    return await conn_or_pool.fetch(query, *params)


async def _fetch_row(
    conn_or_pool: asyncpg.Connection[Any] | asyncpg.Pool,
    query: str,
    *params: Any,
) -> asyncpg.Record | None:
    if isinstance(conn_or_pool, asyncpg.Pool):
        async with conn_or_pool.acquire() as conn:
            return await conn.fetchrow(query, *params)
    return await conn_or_pool.fetchrow(query, *params)


def _to_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


ACCESS_ASSIGNMENTS_SQL = """
SELECT
    nd.id AS device_id,
    di.name AS port,
    iv.access_vlan_id AS vlan_id,
    iv.mode
FROM network_device nd
JOIN device_interface di ON di.device_id = nd.id
JOIN interface_vlan iv ON iv.interface_id = di.id
WHERE iv.mode = 'ACCESS'
  AND iv.access_vlan_id IS NOT NULL
  AND nd.status = 'ACTIVE'
  AND di.admin_status = 'UP'
"""

TRUNK_ASSIGNMENTS_SQL = """
SELECT
    nd.id AS device_id,
    di.name AS port,
    tav.vlan_id AS vlan_id,
    iv.mode
FROM network_device nd
JOIN device_interface di ON di.device_id = nd.id
JOIN interface_vlan iv ON iv.interface_id = di.id
JOIN trunk_allowed_vlan tav ON tav.interface_id = di.id
WHERE iv.mode = 'TRUNK'
  AND nd.status = 'ACTIVE'
  AND di.admin_status = 'UP'
"""

ACCESS_ASSIGNMENTS_BY_VLAN_SQL = ACCESS_ASSIGNMENTS_SQL + "\n  AND iv.access_vlan_id = $1"
TRUNK_ASSIGNMENTS_BY_VLAN_SQL = TRUNK_ASSIGNMENTS_SQL + "\n  AND tav.vlan_id = $1"


async def get_router_subinterfaces(
    conn_or_pool: asyncpg.Connection[Any] | asyncpg.Pool,
) -> dict[int, dict[str, Any]]:
    try:
        rows = await _fetch_rows(
            conn_or_pool,
            """
            SELECT
                di.id,
                di.name,
                di.dot1q_vlan_id,
                di.ip_address,
                di.parent_interface_id,
                nd.id as device_id,
                nd.mgmt_ip
            FROM device_interface di
            JOIN network_device nd ON nd.id = di.device_id
            WHERE di.parent_interface_id IS NOT NULL
              AND di.dot1q_vlan_id IS NOT NULL
              AND nd.device_type = 'ROUTER'
            """,
        )
    except Exception as exc:
        logger.warning("router subinterfaces unavailable: %s", exc)
        return {}

    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        vlan_id = int(row["dot1q_vlan_id"])
        result[vlan_id] = {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "ip_address": str(row["ip_address"]) if row["ip_address"] is not None else None,
            "device_id": str(row["device_id"]),
            "mgmt_ip": str(row["mgmt_ip"]),
        }
    return result


async def get_router_device_id(
    conn_or_pool: asyncpg.Connection[Any] | asyncpg.Pool,
) -> UUID | None:
    try:
        row = await _fetch_row(
            conn_or_pool,
            """
            SELECT id
            FROM network_device
            WHERE device_type = 'ROUTER'
            ORDER BY id
            LIMIT 1
            """,
        )
    except Exception as exc:
        logger.warning("router device lookup failed: %s", exc)
        return None

    if row is None:
        return None
    return _to_uuid(row["id"])


async def get_device_vlans(
    conn_or_pool: asyncpg.Connection[Any] | asyncpg.Pool,
) -> set[tuple[UUID, int]]:
    try:
        rows = await _fetch_rows(conn_or_pool, "SELECT device_id, vlan_id FROM device_vlan")
        return {(_to_uuid(row["device_id"]), int(row["vlan_id"])) for row in rows}
    except Exception as exc:
        logger.warning("device_vlan unavailable: %s", exc)
        return set()


async def get_network_state(conn_or_pool: asyncpg.Connection[Any] | asyncpg.Pool) -> NetworkState:
    device_rows = await _fetch_rows(
        conn_or_pool,
        """
        SELECT id, hostname, mgmt_ip, device_type, status
        FROM network_device
        WHERE status = 'ACTIVE'
        """,
    )
    vlan_rows = await _fetch_rows(
        conn_or_pool,
        """
        SELECT vlan_id, name, admin_status, oper_status,
               COALESCE(is_protected, FALSE) AS is_protected
        FROM vlan
        WHERE admin_status = 'ACTIVE'
""",
    )

    access_rows = await _fetch_rows(conn_or_pool, ACCESS_ASSIGNMENTS_SQL)
    trunk_rows = await _fetch_rows(conn_or_pool, TRUNK_ASSIGNMENTS_SQL)

    assignments: list[VlanAssignment] = []
    for row in [*access_rows, *trunk_rows]:
        payload = dict(row)
        payload["device_id"] = _to_uuid(payload["device_id"])
        assignments.append(VlanAssignment(**payload))
    device_vlans = await get_device_vlans(conn_or_pool)

    vlans = [VlanInfo(**dict(row)) for row in vlan_rows]
    protected_vlans = {int(v.vlan_id) for v in vlans if v.is_protected}
    logger.info("protected vlans loaded: %s", sorted(protected_vlans))

    return NetworkState(
        devices=[Device(**dict(row)) for row in device_rows],
        vlans=vlans,
        assignments=assignments,
        device_vlans=device_vlans,
        protected_vlans=protected_vlans,
        timestamp=datetime.now(timezone.utc),
    )


async def get_assignments_for_vlan(
    conn_or_pool: asyncpg.Connection[Any] | asyncpg.Pool,
    vlan_id: int,
) -> list[VlanAssignment]:
    access_rows = await _fetch_rows(conn_or_pool, ACCESS_ASSIGNMENTS_BY_VLAN_SQL, vlan_id)
    trunk_rows = await _fetch_rows(conn_or_pool, TRUNK_ASSIGNMENTS_BY_VLAN_SQL, vlan_id)

    assignments: list[VlanAssignment] = []
    for row in [*access_rows, *trunk_rows]:
        payload = dict(row)
        payload["device_id"] = _to_uuid(payload["device_id"])
        assignments.append(VlanAssignment(**payload))
    return assignments


# The topology-sync status lives in the dedicated append-only table
# `topology_sync_status` (PK=UUID, written by CM). This is the race-free source
# of truth: it is persistent, so reading the latest row catches the case where
# CM finished syncing before DE even started.
SYNC_STATUS_SQL = """
SELECT status, last_full_sync_at
FROM topology_sync_status
ORDER BY updated_at DESC
LIMIT 1
"""


async def get_sync_state(
    conn_or_pool: asyncpg.Connection[Any] | asyncpg.Pool,
) -> dict[str, Any] | None:
    """Read the latest topology-sync row from ``topology_sync_status``.

    The table is append-only, so ordering by ``updated_at DESC`` and taking the
    first row gives CM's current view. Returns
    ``{"status": <UPPER str>, "last_full_sync_at": <datetime|None>}`` or
    ``None`` when no row exists yet (missing/empty table is treated by callers
    as ``NEVER_SYNCED``). Status values pass through upper-cased: ``OK``,
    ``IN_PROGRESS``, ``FAILED``.
    """
    try:
        row = await _fetch_row(conn_or_pool, SYNC_STATUS_SQL)
    except Exception as exc:
        logger.warning(
            "topology_sync_status unavailable (%s), treating as NEVER_SYNCED",
            exc,
        )
        return None

    if row is None:
        return None

    raw = row["status"]
    status = "NEVER_SYNCED" if raw is None else str(raw).upper()
    return {
        "status": status,
        "last_full_sync_at": row["last_full_sync_at"],
    }


def _normalize_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=_normalize_dsn(settings.postgres_dsn))


async def get_task_batches(
    conn_or_pool: asyncpg.Connection[Any] | asyncpg.Pool,
    task_id: UUID | str,
) -> list[dict[str, Any]]:
    rows = await _fetch_rows(
        conn_or_pool,
        """
        SELECT DISTINCT ON (batch_id)
            task_id,
            batch_id,
            status,
            updated_at
        FROM reconfiguration_task_status
        WHERE task_id = $1
        ORDER BY batch_id, updated_at DESC
        """,
        _to_uuid(task_id),
    )
    return [dict(row) for row in rows]


def get_mock_network_state() -> NetworkState:
    d1 = uuid4()
    d2 = uuid4()
    return NetworkState(
        devices=[
            Device(
                id=d1,
                hostname="sw-1",
                mgmt_ip="10.0.0.1",
                device_type="SWITCH",
                status="ACTIVE",
            ),
            Device(
                id=d2,
                hostname="sw-2",
                mgmt_ip="10.0.0.2",
                device_type="SWITCH",
                status="ACTIVE",
            ),
        ],
        vlans=[
            VlanInfo(vlan_id=10, name="users", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=20, name="servers", admin_status="ACTIVE", oper_status="UP"),
            VlanInfo(vlan_id=30, name="guests", admin_status="ACTIVE", oper_status="UP"),
        ],
        assignments=[
            VlanAssignment(device_id=d1, port="Gi0/1", vlan_id=20, mode="ACCESS"),
            VlanAssignment(device_id=d1, port="Gi0/2", vlan_id=10, mode="ACCESS"),
            VlanAssignment(device_id=d2, port="Gi0/1", vlan_id=20, mode="ACCESS"),
            VlanAssignment(device_id=d2, port="Gi0/2", vlan_id=30, mode="ACCESS"),
        ],
        device_vlans={(d1, 10), (d1, 20), (d2, 20), (d2, 30)},
        timestamp=datetime.now(timezone.utc),
    )


# --- Topology loading -------------------------------------------------------

LINKS_SQL = "SELECT interface_a_id, interface_b_id FROM link"
ENDPOINT_ATTACH_SQL = "SELECT network_interface_id FROM endpoint_network_attachment"
DEVICE_INTERFACES_SQL = (
    "SELECT id, device_id, name, parent_interface_id, ip_address, dot1q_vlan_id "
    "FROM device_interface"
)
INTERFACE_MODES_SQL = "SELECT interface_id, mode, access_vlan_id FROM interface_vlan"
TRUNK_ALLOWED_ALL_SQL = "SELECT interface_id, vlan_id FROM trunk_allowed_vlan"
DEVICE_VLAN_ALL_SQL = "SELECT device_id, vlan_id FROM device_vlan"
PROTECTED_VLANS_SQL = "SELECT vlan_id FROM vlan WHERE COALESCE(is_protected, FALSE) = TRUE"


async def load_topology(
    conn_or_pool: asyncpg.Connection[Any] | asyncpg.Pool,
) -> "Topology | None":
    """Build the L2 topology graph from PostgreSQL.

    Reads `link` and `endpoint_network_attachment` and reuses the device /
    interface / VLAN tables already consumed for NetworkState. Returns a fully
    built :class:`Topology`, or ``None`` when the topology is empty/incomplete or
    unavailable so that TaskBuilder gracefully falls back to legacy single
    actions.
    """
    from src.topology.graph import Topology

    try:
        link_rows = await _fetch_rows(conn_or_pool, LINKS_SQL)
    except Exception as exc:
        logger.warning(
            "topology link table unavailable (%s), falling back to legacy single actions",
            exc,
        )
        return None

    links = [
        (_to_uuid(row["interface_a_id"]), _to_uuid(row["interface_b_id"]))
        for row in link_rows
    ]
    if not links:
        logger.warning("topology empty/incomplete, falling back to legacy single actions")
        return None

    try:
        iface_rows = await _fetch_rows(conn_or_pool, DEVICE_INTERFACES_SQL)
        mode_rows = await _fetch_rows(conn_or_pool, INTERFACE_MODES_SQL)
        allowed_rows = await _fetch_rows(conn_or_pool, TRUNK_ALLOWED_ALL_SQL)
        device_vlan_rows = await _fetch_rows(conn_or_pool, DEVICE_VLAN_ALL_SQL)
        endpoint_rows = await _fetch_rows(conn_or_pool, ENDPOINT_ATTACH_SQL)
        protected_rows = await _fetch_rows(conn_or_pool, PROTECTED_VLANS_SQL)
    except Exception as exc:
        logger.warning(
            "topology load failed (%s), falling back to legacy single actions",
            exc,
        )
        return None

    mode_by_iface: dict[UUID, str] = {}
    access_by_iface: dict[UUID, int | None] = {}
    for row in mode_rows:
        iface_id = _to_uuid(row["interface_id"])
        mode_by_iface[iface_id] = str(row["mode"]).upper()
        access_by_iface[iface_id] = row["access_vlan_id"]

    allowed_ifaces = {_to_uuid(row["interface_id"]) for row in allowed_rows}

    interfaces: list[dict[str, Any]] = []
    for row in iface_rows:
        iface_id = _to_uuid(row["id"])
        parent = row["parent_interface_id"]
        parent_id = _to_uuid(parent) if parent is not None else None
        ip_address = str(row["ip_address"]) if row["ip_address"] is not None else None
        mode = mode_by_iface.get(iface_id)
        if mode is None:
            if iface_id in allowed_ifaces:
                mode = "TRUNK"
            elif parent_id is not None or ip_address is not None:
                # L3 routed interface / dot1q subinterface -> no switchport mode.
                mode = None
            else:
                mode = "ACCESS"
        interfaces.append(
            {
                "interface_id": iface_id,
                "device_id": _to_uuid(row["device_id"]),
                "name": row["name"],
                "mode": mode,
                "access_vlan_id": access_by_iface.get(iface_id),
                "parent_interface_id": parent_id,
                "ip_address": ip_address,
                "dot1q_vlan_id": row["dot1q_vlan_id"],
            }
        )

    trunk_allowed = [
        (_to_uuid(row["interface_id"]), int(row["vlan_id"])) for row in allowed_rows
    ]
    device_vlan = [
        (_to_uuid(row["device_id"]), int(row["vlan_id"])) for row in device_vlan_rows
    ]
    endpoints = {_to_uuid(row["network_interface_id"]) for row in endpoint_rows}
    protected_vlans = {int(row["vlan_id"]) for row in protected_rows}

    topology = Topology.build(
        interfaces=interfaces,
        links=links,
        trunk_allowed=trunk_allowed,
        device_vlan=device_vlan,
        endpoints=endpoints,
        protected_vlans=protected_vlans,
    )
    logger.info(
        "topology loaded: devices=%d links=%d endpoints=%d protected_vlans=%s",
        topology.graph.number_of_nodes(),
        len(topology.links),
        len(endpoints),
        sorted(protected_vlans),
    )
    return topology
