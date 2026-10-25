"""The migrations and models.py must describe the same schema (needs TEST_DATABASE_URL)."""


def test_models_match_migrations(run_alembic):
    """Fails if models.py was changed without a migration (the same check as `alembic check`)."""
    run_alembic("check")
