"""Backwards-compatible re-export.

The topology graph now lives in :mod:`src.topology.graph` (strategy/domain
layer). ``task_builder`` is a *consumer* of it, not the owner. This shim keeps
existing import paths working.
"""

from src.topology.graph import (  # noqa: F401
    ACCESS,
    TRUNK,
    InterfaceInfo,
    Link,
    Topology,
    build_trunk_action,
    required_links,
    set_trunk_action,
    trunk_actions_for_link,
)

__all__ = [
    "ACCESS",
    "TRUNK",
    "InterfaceInfo",
    "Link",
    "Topology",
    "build_trunk_action",
    "required_links",
    "set_trunk_action",
    "trunk_actions_for_link",
]
