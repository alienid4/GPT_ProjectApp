from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def database(url, mode):
    driver = make_url(url).get_backend_name()
    if mode == "production" and driver != "mssql":
        raise RuntimeError("Production requires the configured SQL Server database.")
    if mode not in {"production", "test"}:
        raise RuntimeError("APP_MODE must explicitly be production or test.")
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if driver == "sqlite":
        if mode != "test":
            raise RuntimeError("SQLite is restricted to the isolated test environment.")
        options["connect_args"] = {"check_same_thread": False, "timeout": 20}
        if make_url(url).database in {None, "", ":memory:"}:
            options["poolclass"] = StaticPool
    engine = create_engine(url, **options)
    if driver == "sqlite":
        @event.listens_for(engine, "connect")
        def configure(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=3000")
            cursor.close()
    elif driver == 'mssql':
        @event.listens_for(engine, 'connect')
        def configure_sqlserver(connection, _):
            cursor = connection.cursor()
            cursor.execute('SET LOCK_TIMEOUT 3000')
            cursor.close()
    return engine, sessionmaker(engine, expire_on_commit=False)
