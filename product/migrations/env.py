"""Explicit database configuration; no credentials stored in the repository."""
import os
from alembic import context
from workboard.database import database
from workboard.models import Base

url = os.environ.get('DATABASE_URL')
mode = os.environ.get('APP_MODE', 'production')
if not url:
    raise RuntimeError('DATABASE_URL must be configured by the operator.')

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True,
                      dialect_opts={'paramstyle': 'named'}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine, _ = database(url, mode)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
