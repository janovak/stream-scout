"""Gunicorn server hooks for the API & Frontend service."""

import logging

logger = logging.getLogger("gunicorn.error")


def post_worker_init(worker):
    """Warm the database connection pool before this worker takes traffic.

    Without this hook, the pool opens on the first request that touches
    the database. The user who sends that request pays the full
    connection-setup cost. This cost is higher when Postgres is not on
    this machine.

    This hook runs after the worker forks and loads the app. Psycopg2
    cannot share connections across a fork. This order makes the call
    safe.

    A failed connection here must not crash the worker. Gunicorn treats
    an exception at this point as a boot failure and stops the whole
    server, not just this worker. If Postgres is not reachable yet, log
    a warning instead and let the pool open lazily on the first request,
    the same as it did before this hook existed.
    """
    from api_frontend_service import get_db_pool

    try:
        get_db_pool()
    except Exception:
        logger.warning(
            "Could not warm the database pool at worker boot. "
            "The pool will open on the first request instead.",
            exc_info=True,
        )
