"""Compatibility exports for local identity services."""

from aigineering.local_identity import (
    LOCAL_ROOT_CAPABILITIES,
    actor_key_path,
    ensure_local_domain,
    ensure_local_plugin_publisher,
    ensure_local_runtime_publishers,
    ensure_local_worker_host,
    load_actor_signer,
    write_actor_key,
)

__all__ = (
    "LOCAL_ROOT_CAPABILITIES",
    "actor_key_path",
    "ensure_local_domain",
    "ensure_local_plugin_publisher",
    "ensure_local_runtime_publishers",
    "ensure_local_worker_host",
    "load_actor_signer",
    "write_actor_key",
)
