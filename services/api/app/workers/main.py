from __future__ import annotations

import signal
import sys
import threading
from typing import Mapping, Sequence

from sqlalchemy import Engine, create_engine

from app.config import Settings, load_settings
from app.workers.worker import DurableWorker, Handler

# Domain job handlers are registered by later composition wiring. The durable
# worker primitive runs with whatever registry it is given; unknown kinds are
# retried and eventually dead-lettered by policy rather than lost.
DEFAULT_HANDLERS: dict[str, Handler] = {}


def build_engine(settings: Settings) -> Engine:
    if settings.database_url is None:
        raise ValueError("DATABASE_URL is required to run the durable worker")
    return create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)


def build_worker(
    engine: Engine,
    *,
    handlers: Mapping[str, Handler] | None = None,
    worker_id: str | None = None,
) -> DurableWorker:
    return DurableWorker(engine, handlers=handlers or DEFAULT_HANDLERS, worker_id=worker_id)


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)


def main(argv: Sequence[str] | None = None) -> int:
    settings = load_settings()
    if settings.database_url is None:
        print(
            "[failed] worker: DATABASE_URL is not configured; background jobs and "
            "synchronization are unavailable",
            file=sys.stderr,
        )
        return 1

    engine = build_engine(settings)
    worker = build_worker(engine)
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    print(f"[ready] worker: durable outbox worker {worker.worker_id} started", flush=True)
    try:
        worker.run_forever(stop_event)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        engine.dispose()
        print("[stopped] worker: durable outbox worker shut down", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
