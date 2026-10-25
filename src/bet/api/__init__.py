"""HTTP API over the warehouse (ADR 0002).

One process owns the DuckDB file, so the server runs exactly one worker and
refuses to start with more. Run it with the ``bet-api`` console script:

    bet-api                        # 127.0.0.1:8000
    bet-api --host 0.0.0.0 --port 8080

The warehouse is the same one the CLI uses -- ``BET_DATA_DIR``, ``BET_DB_PATH``
and the config file resolve exactly as they do for ``bet``. It must already
exist and be migrated (``bet init``); ``/readyz`` reports pending migrations.
``BET_JWT_SIGNING_KEY`` must be set, or the server refuses to start (see
``bet.api.settings``); users and passwords come from ``bet user``.
``BET_MAINTENANCE_TOKEN``, if set, opens the backup routes under
``/maintenance`` (see ``bet.api.maintenance``); unset, they answer 404.
OpenAPI is served at ``/docs`` and ``/openapi.json``.
"""

from bet.api.app import create_app
from bet.api.settings import ApiSettings

__all__ = ["ApiSettings", "create_app"]
