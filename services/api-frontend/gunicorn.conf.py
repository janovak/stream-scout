"""Gunicorn server hooks for the API & Frontend service."""


def post_worker_init(worker):
    """Warm the database connection pool before this worker takes traffic.

    Without this, the pool opens lazily on the first request that touches
    the database, and whichever user sends that request pays the full
    connection-setup cost -- worse when Postgres is not on this machine.
    Runs after the worker forks and loads the app, so it is safe with
    psycopg2, which cannot share connections across a fork.
    """
    from api_frontend_service import get_db_pool

    get_db_pool()
