"""aig serve — start the Aigineering API server (lazy import)."""

from __future__ import annotations

import click


@click.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address.")
@click.option("--port", type=int, default=8000, show_default=True, help="Bind port.")
@click.option(
    "--reload",
    is_flag=True,
    default=False,
    help="Enable auto-reload for development.",
)
def serve(host: str, port: int, reload: bool) -> None:
    """Start the Aigineering API server (FastAPI + uvicorn).

    Requires the ``api`` extra: ``pip install aigineering[api]``
    """
    try:
        import uvicorn
    except ImportError:
        raise click.ClickException(
            "uvicorn is not installed. Install with: pip install aigineering[api]"
        )

    click.echo(f"Starting Aigineering API server on {host}:{port}")
    uvicorn.run(
        "aigineering.server.app:app",
        host=host,
        port=port,
        reload=reload,
    )
