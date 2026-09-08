"""Run Studio on the container network while preserving graceful shutdown."""

from __future__ import annotations

import os

import uvicorn

from studio_server import StudioUvicornServer, _shutdown_requested, app


def main() -> None:
    host = os.environ.get("INDEXTTS_STUDIO_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.environ.get("INDEXTTS_STUDIO_PORT", "7860"))
    _shutdown_requested.clear()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=5,
    )
    StudioUvicornServer(config).run()


if __name__ == "__main__":
    main()
