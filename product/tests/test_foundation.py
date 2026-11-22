from datetime import datetime

import pytest
from sqlalchemy import insert
from sqlalchemy.dialects import mssql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.schema import CreateTable

from workboard.database import database
from workboard.models import Base, Person, Task, TaskAccess, retention_end


def fixtures():
    engine, sessions = database("sqlite://", "test")
    Base.metadata.create_all(engine)
    with sessions.begin() as db:
        person = Person(directory_id="directory-object-1", username="author", name="測試人員", department="TEST")
        db.add(person)
        db.flush()
        task = Task(code="WK-TEST", title="待辦", department="TEST", creator_id=person.id, owner_id=person.id)
        db.add(task)
        db.flush()
        db.add(TaskAccess(task_id=task.id))
        identifiers = person.id, task.id
    return engine, sessions, identifiers


def test_production_cannot_silently_use_test_database():
    with pytest.raises(RuntimeError, match="SQL Server"):
        database("sqlite://", "production")
    with pytest.raises(RuntimeError, match="explicitly"):
        database("sqlite://", "development")


def test_schema_has_sql_server_compilable_constraints():
    for table in Base.metadata.sorted_tables:
        sql = str(CreateTable(table).compile(dialect=mssql.dialect()))
        assert "CREATE TABLE" in sql
    assert "ck_task_status" in str(CreateTable(Task.__table__).compile(dialect=mssql.dialect()))


def test_foreign_keys_and_state_constraints_reject_invalid_records():
    engine, sessions, (person, task_id) = fixtures()
    with pytest.raises(IntegrityError), sessions.begin() as db:
        db.add(Task(code="WK-BAD", title="無效", department="TEST", creator_id="missing-person", status="done"))
    with pytest.raises(IntegrityError), sessions.begin() as db:
        db.add(Task(code="WK-BADSTATE", title="無效", department="TEST", creator_id=person, status="anything"))
    engine.dispose()


def test_conflicting_content_writes_reject_the_stale_writer():
    engine, sessions, (_, task_id) = fixtures()
    a, b = sessions(), sessions()
    try:
        first, stale = a.get(Task, task_id), b.get(Task, task_id)
        first.result = "第一位已儲存"
        a.commit()
        stale.result = "不可覆蓋第一位"
        with pytest.raises(StaleDataError):
            b.commit()
        b.rollback()
        assert b.get(Task, task_id).result == "第一位已儲存"
    finally:
        a.close()
        b.close()
        engine.dispose()


def test_acl_revision_does_not_revise_content_or_reset_retention():
    engine, sessions, (_, task_id) = fixtures()
    with sessions.begin() as db:
        task, access = db.get(Task, task_id), db.get(TaskAccess, task_id)
        before = task.revision, task.created_at, task.expires_at
        access.confidential = True
    with sessions.begin() as db:
        task, access = db.get(Task, task_id), db.get(TaskAccess, task_id)
        assert (task.revision, task.created_at, task.expires_at) == before
        assert access.revision == 2
        task.title = "更名"
    with sessions() as db:
        task = db.get(Task, task_id)
        assert task.created_at == before[1] and task.expires_at == before[2]
    engine.dispose()


def test_three_calendar_years_including_leap_day():
    assert retention_end(datetime(2024, 2, 29, 13, 20)) == datetime(2027, 2, 28, 13, 20)
    assert retention_end(datetime(2026, 11, 22)) == datetime(2029, 11, 22)
