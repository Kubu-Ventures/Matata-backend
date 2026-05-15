import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.core.config import settings
from app.db.base import Base

# Alembic Config object — provides access to values in alembic.ini
config = context.config

# Override sqlalchemy.url with the value from pydantic-settings.
# This means alembic.ini never needs a hardcoded connection string.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Set up loggers as defined in alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate support.
# All models must be imported (directly or via app.db.base) before
# Alembic can detect them — importing Base here is sufficient if
# every model module is imported inside app/db/base.py.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in offline mode.

    Configures the context with a URL rather than an Engine.
    Useful for generating SQL scripts without a live database connection.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in online mode using an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())