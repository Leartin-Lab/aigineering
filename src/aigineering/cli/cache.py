"""Compatibility name for required Redis maintenance commands."""

import click

from aigineering.cli.redis import redis_rebuild, redis_status, redis_sync


@click.group("cache")
def cache_group() -> None:
    """Compatibility alias; use 'aig redis' for the required runtime component."""


cache_group.add_command(redis_status)
cache_group.add_command(redis_sync)
cache_group.add_command(redis_rebuild)
