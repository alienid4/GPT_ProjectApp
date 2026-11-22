from pathlib import Path
import io
import pytest
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from workboard.database import database
from workboard.models import Base

ROOT = Path(__file__).resolve().parents[1]


def test_frozen_migration_is_complete_repeatable_and_preserves_rows(tmp_path, monkeypatch):
    url = 'sqlite:///' + (tmp_path / 'schema.sqlite').as_posix()
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.setenv('APP_MODE', 'test')
    cfg = Config(str(ROOT / 'alembic.ini'))
    command.upgrade(cfg, 'head')
    engine, _ = database(url, 'test')
    with engine.begin() as conn:
        assert not compare_metadata(MigrationContext.configure(conn), Base.metadata)
        conn.execute(text("INSERT INTO sequences (key,value) VALUES ('migration_preservation',42)"))
    command.upgrade(cfg, 'head')
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT value FROM sequences WHERE key='migration_preservation'")) == 42
    with pytest.raises(RuntimeError, match='Destructive'):
        command.downgrade(cfg, 'base')
    assert 'people' in inspect(engine).get_table_names()
    engine.dispose()


def test_sqlserver_migration_compiles_offline_only(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'mssql+pyodbc://migration-placeholder')
    monkeypatch.setenv('APP_MODE', 'production')
    output = io.StringIO()
    cfg = Config(str(ROOT / 'alembic.ini'), output_buffer=output)
    command.upgrade(cfg, 'head', sql=True)
    ddl = output.getvalue()
    assert 'CREATE TABLE people' in ddl and 'NVARCHAR' in ddl and 'alembic_version' in ddl
    # Compilation is not a live SQL Server integration test.
