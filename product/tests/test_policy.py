import pytest
from sqlalchemy import select

from workboard.database import database
from workboard.models import (AccessReader, Base, CapabilityGrant, Person, Task,
                              TaskAccess, TaskRelation)
from workboard.policy import (can_cancel, can_edit, can_finalize, can_manage,
                              can_read, task_read_clause)


@pytest.fixture
def data():
    engine, sessions = database("sqlite://", "test")
    Base.metadata.create_all(engine)
    db = sessions()
    people = {}
    for name, role in [("a", "member"), ("b", "member"), ("unrelated", "member"), ("chief", "chief"), ("admin", "maintenance")]:
        p = Person(directory_id=name, username=name, name=name, department="D", role=role)
        db.add(p)
        people[name] = p
    db.flush()
    task = Task(code="WK-1", title="協作", department="D", creator_id=people["chief"].id,
                owner_id=people["a"].id, source="assigned")
    db.add(task)
    db.flush()
    access = TaskAccess(task_id=task.id)
    db.add_all([access, TaskRelation(task_id=task.id, person_id=people["b"].id, kind="collaborator")])
    db.commit()
    yield db, people, task, access
    db.close()
    engine.dispose()


def test_related_staff_only_and_maintenance_is_not_a_content_superuser(data):
    db, p, t, acl = data
    assert can_read(db, p["a"], t) and can_read(db, p["b"], t)
    assert can_read(db, p["chief"], t)
    assert not can_read(db, p["unrelated"], t)
    assert not can_read(db, p["admin"], t)
    assert list(db.scalars(select(Task).where(task_read_clause(p["unrelated"])))) == []


def test_confidential_list_overrides_owner_collaborator_and_manager(data):
    db, p, t, acl = data
    acl.confidential = True
    db.add(AccessReader(task_id=t.id, person_id=p["a"].id, kind="confidential"))
    db.commit()
    assert can_read(db, p["a"], t)
    for who in ["b", "chief", "admin"]:
        assert not can_read(db, p[who], t)
        assert not can_manage(db, p[who], t, "share")


def test_collaboration_does_not_grant_cancellation_or_confirmation(data):
    db, p, t, acl = data
    assert can_edit(db, p["b"], t)
    assert not can_finalize(db, p["b"], t)
    assert not can_cancel(db, p["a"], t)
    assert not can_manage(db, p["a"], t, "confirm")
    assert can_manage(db, p["chief"], t, "confirm")
    assert not can_edit(db, p["chief"], t)


@pytest.mark.parametrize('relationship', ['collaborator', 'share', 'confidential'])
def test_access_to_one_task_never_grants_access_to_another_task(data, relationship):
    db, people, shared_task, shared_acl = data
    db.delete(db.get(TaskRelation, (shared_task.id, people['b'].id, 'collaborator')))
    private = Task(code='PRIVATE-2', title='另一筆機密', department='D', creator_id=people['a'].id,
                   owner_id=people['a'].id, source='self')
    db.add(private)
    db.flush()
    db.add(TaskAccess(task_id=private.id, confidential=relationship == 'confidential'))
    if relationship == 'collaborator':
        db.add(TaskRelation(task_id=shared_task.id, person_id=people['b'].id, kind='collaborator'))
    else:
        shared_acl.confidential = relationship == 'confidential'
        db.add(AccessReader(task_id=shared_task.id, person_id=people['b'].id, kind=relationship))
    db.commit()
    assert can_read(db, people['b'], shared_task)
    assert not can_read(db, people['b'], private)
    visible = list(db.scalars(select(Task.id).where(task_read_clause(people['b']))))
    assert visible == [shared_task.id]


@pytest.mark.parametrize('dialect_name', ['sqlite', 'mssql'])
def test_nested_access_queries_only_reference_the_outer_task(data, dialect_name):
    from sqlalchemy.dialects import sqlite, mssql
    _, people, _, _ = data
    dialect = sqlite.dialect() if dialect_name == 'sqlite' else mssql.dialect()
    sql = str(select(Task.id).where(task_read_clause(people['b'])).compile(
        dialect=dialect, compile_kwargs={'literal_binds': True}))
    assert sql.count('FROM tasks') == 1
    assert 'FROM access_readers, tasks' not in sql and 'FROM task_relations, tasks' not in sql
    assert sql.count('access_readers.task_id = tasks.id') == 2
    assert 'task_relations.task_id = tasks.id' in sql
    # MSSQL compilation is a guard against generated-query regression, not a live DB test.


def test_revocation_and_deactivation_take_effect_for_subsequent_reads(data):
    db, p, t, acl = data
    relation = db.get(TaskRelation, (t.id, p["b"].id, "collaborator"))
    db.delete(relation)
    db.commit()
    assert not can_read(db, p["b"], t)
    p["a"].active = False
    db.commit()
    assert not can_read(db, p["a"], t)
    assert not can_edit(db, p["a"], t)


def test_capability_does_not_share_content_and_scopes_do_not_bleed(data):
    db, p, t, acl = data
    db.add(CapabilityGrant(person_id=p["unrelated"].id, scope_id=t.id,
                           capability="dispatch", granted_by=p["chief"].id))
    db.add(CapabilityGrant(person_id=p["b"].id, scope_id="different-scope",
                           capability="cancel", granted_by=p["chief"].id))
    db.commit()
    assert not can_read(db, p["unrelated"], t)
    assert not can_manage(db, p["unrelated"], t, "dispatch")
    assert not can_cancel(db, p["b"], t)
