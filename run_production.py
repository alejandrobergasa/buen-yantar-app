import os

import config
from waitress import serve

from app import create_app


def main() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    threads_default = "2" if config.LOW_RESOURCE_MODE else "4"
    threads = int(os.getenv("WAITRESS_THREADS", threads_default))
    connection_limit_default = "40" if config.LOW_RESOURCE_MODE else "100"
    connection_limit = int(os.getenv("WAITRESS_CONNECTION_LIMIT", connection_limit_default))

    app = create_app()
    serve(
        app,
        host=host,
        port=port,
        threads=threads,
        connection_limit=connection_limit,
        cleanup_interval=30,
        channel_timeout=30,
        ident="BuenYantar",
    )


if __name__ == "__main__":
    main()
