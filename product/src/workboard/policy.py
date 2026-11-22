"""Current-record authorization, shared by HTTP, exports and storage.

An administrative or managerial role never grants confidential read access.
"""
import json
from sqlalchemy import and_, exists, or_, select

from .errors import DomainError
from .models import (AccessReader, CapabilityGrant, Coordination, Person, Project,
                     ProjectMember, Task, TaskAccess, TaskRelation, VersionReader,
                     utcnow)


ROLE_CAPABILITIES = {
    "member": frozenset(),
    "chief": frozenset({"department_view", "dispatch", "priority", "confirm",
                        "commit", "cancel", "pause", "share", "confidential",
                        "grant", "comment", "capacity"}),
    "director": frozenset({"department_view", "priority"}),
    "maintenance": frozenset({"settings", "users", "templates", "backup"}),
    "cleaner": frozenset({"cleanup"}),
}
WORK_ROLES = {"member", "chief", "director"}


def enabled(actor):
    return actor is not None and actor.active and actor.directory_active


def actor_roles(actor):
    if not enabled(actor):
        return set()
    return configured_roles(actor)


def configured_roles(actor):
    """Display saved configuration without treating a disabled user as enabled."""
    if actor is None:
        return set()
    try:
        extra = json.loads(actor.roles_json or "[]")
    except (ValueError, TypeError):
        return set()
    if not isinstance(extra, list):
        return set()
    return {r for r in [actor.role, *extra] if isinstance(r, str) and r in ROLE_CAPABILITIES}


def global_capability(actor, capability):
    return any(capability in ROLE_CAPABILITIES.get(role, ()) for role in actor_roles(actor))


def work_eligible(actor):
    return bool(actor_roles(actor) & WORK_ROLES)


def task_scope(task):
    return task.project_id or task.id


def has_capability(db, actor, task, capability):
    if not enabled(actor) or actor.department != task.department:
        return False
    if global_capability(actor, capability):
        return True
    return db.scalar(select(exists().where(
        CapabilityGrant.person_id == actor.id,
        or_(and_(CapabilityGrant.scope_type == 'task', CapabilityGrant.scope_id == task.id),
            and_(CapabilityGrant.scope_type == 'project', CapabilityGrant.scope_id == task.project_id)),
        CapabilityGrant.capability == capability,
        or_(CapabilityGrant.valid_until.is_(None), CapabilityGrant.valid_until > utcnow()),
    )))


def current_relation(db, actor, task, kinds):
    if not enabled(actor):
        return False
    return db.scalar(select(exists().where(
        TaskRelation.task_id == task.id, TaskRelation.person_id == actor.id,
        TaskRelation.kind.in_(kinds),
        or_(TaskRelation.valid_until.is_(None), TaskRelation.valid_until > utcnow()),
    )))


def task_read_clause(actor):
    """Filter before counts, grouping, pagination or search; never filter afterward."""
    if not work_eligible(actor):
        return Task.id.is_(None)
    active_relation = exists().where(
        TaskRelation.task_id == Task.id, TaskRelation.person_id == actor.id,
        TaskRelation.kind.in_(["collaborator", "delegate"]),
        or_(TaskRelation.valid_until.is_(None), TaskRelation.valid_until > utcnow()),
    ).correlate(Task)
    shared = exists().where(AccessReader.task_id == Task.id,
                            AccessReader.person_id == actor.id, AccessReader.kind == "share").correlate(Task)
    secret_reader = exists().where(AccessReader.task_id == Task.id,
                                   AccessReader.person_id == actor.id, AccessReader.kind == "confidential").correlate(Task)
    # Explicit correlation must reach the outer task through TaskAccess.
    # Otherwise a relation to ANY task can satisfy this nested EXISTS.
    related = or_(Task.owner_id == actor.id, active_relation, shared,
                  and_(Task.assignment == "unassigned", Task.creator_id == actor.id),
                  and_(Task.assignment == "proposed", or_(Task.creator_id == actor.id, Task.proposed_owner_id == actor.id)))
    if global_capability(actor, "department_view"):
        related = or_(related, Task.department == actor.department)
    classification = exists().where(TaskAccess.task_id == Task.id,
                                    or_(and_(TaskAccess.confidential.is_(True), secret_reader),
                                        and_(TaskAccess.confidential.is_(False), related))).correlate(Task)
    return and_(Task.deleted.is_(False), Task.department == actor.department, classification)


def can_read(db, actor, task):
    if task is None:
        return False
    return db.scalar(select(Task.id).where(Task.id == task.id, task_read_clause(actor))) is not None


def readable_task(db, actor, task_id):
    task = db.scalar(select(Task).where(Task.id == task_id, task_read_clause(actor)))
    if task is None:
        raise DomainError("找不到工作。", 404, "not_found")
    return task


def can_edit(db, actor, task):
    return can_read(db, actor, task) and (
        task.owner_id == actor.id or current_relation(db, actor, task, ["collaborator", "delegate"]))


def can_finalize(db, actor, task):
    return can_read(db, actor, task) and (
        task.owner_id == actor.id or current_relation(db, actor, task, ["delegate"]))


def can_comment(db, actor, task):
    return can_read(db, actor, task) and (
        can_edit(db, actor, task) or has_capability(db, actor, task, "comment") or
        (task.assignment == "proposed" and actor.id in {task.creator_id, task.proposed_owner_id}) or
        db.scalar(select(exists().where(Coordination.resource_type == "task",
                                        Coordination.resource_id == task.id,
                                        Coordination.status.in_(["pending", "accepted"]),
                                        or_(Coordination.creator_id == actor.id,
                                            Coordination.assignee_id == actor.id)))))


def can_manage(db, actor, task, capability):
    return can_read(db, actor, task) and has_capability(db, actor, task, capability)


def own_independent_work(db, actor, task):
    if not can_read(db, actor, task) or task.source != "self" or task.owner_id != actor.id or task.creator_id != actor.id:
        return False
    return not db.scalar(select(exists().where(Task.parent_id == task.id, Task.deleted.is_(False))))


def can_cancel(db, actor, task):
    return can_manage(db, actor, task, "cancel") or own_independent_work(db, actor, task)


def can_pause(db, actor, task):
    return can_manage(db, actor, task, "pause") or own_independent_work(db, actor, task)


def can_read_version_record(db, actor, task, version):
    if task is None or version is None or version.task_id != task.id or not can_read(db, actor, task):
        return False
    return not version.restricted or db.get(VersionReader, (version.id, actor.id)) is not None


def can_read_version(db, actor, task, version):
    return can_read_version_record(db, actor, task, version) and not version.deleted


def can_read_project(db, actor, project):
    if not work_eligible(actor) or project is None or actor.department != project.department:
        return False
    if project.owner_id == actor.id or db.get(ProjectMember, (project.id, actor.id)):
        return True
    return db.scalar(select(Task.id).where(Task.project_id == project.id, task_read_clause(actor)).limit(1)) is not None


def can_plan_project(db, actor, project):
    return can_read_project(db, actor, project) and (
        project.owner_id == actor.id or db.get(ProjectMember, (project.id, actor.id)) is not None)


def task_permissions(db, actor, task):
    """UI hints only. Every mutation independently calls these checks again."""
    return {
        "edit": can_edit(db, actor, task), "finalize": can_finalize(db, actor, task),
        "comment": can_comment(db, actor, task), "cancel": can_cancel(db, actor, task),
        "pause": can_pause(db, actor, task),
        **{key: can_manage(db, actor, task, key) for key in
           ["dispatch", "priority", "confirm", "commit", "share", "confidential", "grant"]},
    }
