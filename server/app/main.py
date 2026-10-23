"""Entry point: the API on one port, the signed-webhook receiver on another.

The webhook server runs in its own thread so that only the main thread's uvicorn
handles SIGTERM (what `docker stop` sends) and both shut down together.
"""

from __future__ import annotations

import logging
import threading

import uvicorn

from .api import create_api_app
from .bootstrap import build_services
from .config import load_settings
from .webhook import WebhookVerifier, create_webhook_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    try:
        services = build_services(settings)
    except ValueError as exc:
        raise SystemExit(f"Cannot start: {exc}") from None
    if services.plaid is None:
        logging.warning("PLAID_CLIENT_ID / PLAID_SECRET not set: banks cannot be linked or synced.")
    if not services.auth.has_user():
        logging.warning("No login yet. Run: docker exec -it homefinance python -m app.cli create-user --username YOU")

    api = uvicorn.Server(
        uvicorn.Config(
            create_api_app(services),
            host=settings.host,
            port=settings.api_port,
            access_log=False,  # request lines can reveal what you looked at
            server_header=False,
        )
    )
    hook = None
    if services.plaid is not None:
        verifier = WebhookVerifier(services.plaid)
        hook = uvicorn.Server(
            uvicorn.Config(
                create_webhook_app(services.db, services.sync, verifier),
                host=settings.host,
                port=settings.webhook_port,
                access_log=False,
                server_header=False,
            )
        )
        threading.Thread(target=hook.run, name="webhook", daemon=True).start()
    services.sync.start()
    services.backups.start()
    services.performance.start()
    try:
        api.run()
    finally:
        if hook is not None:
            hook.should_exit = True
        services.performance.stop()
        services.backups.stop()
        services.sync.stop()
        services.db.close()


if __name__ == "__main__":
    main()
