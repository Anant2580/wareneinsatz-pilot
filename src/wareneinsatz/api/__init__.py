"""Step 8: the REST API behind the web app. `create_app()` builds it; `wareneinsatz serve` runs it."""

from wareneinsatz.api.app import create_app

__all__ = ["create_app"]
