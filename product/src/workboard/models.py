"""Portable relational model; production target is SQL Server.

UTC timestamps are stored without an offset and explicitly serialized as UTC.
ACL revisions are stored separately from work revisions.
"""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, ForeignKey,
                        Integer, LargeBinary, Numeric, String, Unicode, Index,
                        UnicodeText, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, declared_attr


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def retention_end(created):
    try:
        return created.replace(year=created.year + 3)
    except ValueError:
        return created.replace(year=created.year + 3, day=28)


def new_id():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda ctx: retention_end(ctx.get_current_parameters().get("created_at") or utcnow()))


class Revision:
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    @declared_attr.directive
    def __mapper_args__(cls):
        return {"version_id_col": cls.revision}


class Person(Record, Revision, Base):
    __tablename__ = "people"
    directory_id: Mapped[str] = mapped_column(String(128), unique=True)
    username: Mapped[str] = mapped_column(Unicode(120), unique=True)
    name: Mapped[str] = mapped_column(Unicode(120))
    department: Mapped[str] = mapped_column(Unicode(120), index=True)
    role: Mapped[str] = mapped_column(String(30), default="member")
    roles_json: Mapped[str] = mapped_column(UnicodeText, default="[]")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    directory_active: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_epoch: Mapped[int] = mapped_column(Integer, default=1)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    auth_epoch: Mapped[int] = mapped_column(Integer)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class SecurityEvent(Record, Base):
    __tablename__ = "security_events"
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    route: Mapped[str] = mapped_column(String(120))
    outcome: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(80))
    address_hash: Mapped[str] = mapped_column(String(64))


class Project(Record, Revision, Base):
    __tablename__ = "projects"
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(Unicode(200))
    goal: Mapped[str] = mapped_column(UnicodeText, default="")
    department: Mapped[str] = mapped_column(Unicode(120), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="todo")
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    cleaned: Mapped[bool] = mapped_column(Boolean, default=False)
    template_snapshot: Mapped[str] = mapped_column(UnicodeText, default="{}")
    next_action: Mapped[str] = mapped_column(UnicodeText, default="")
    wait_for: Mapped[str] = mapped_column(UnicodeText, default="")
    follow_on: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    current_submission_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), primary_key=True)


class ProjectSubmission(Record, Base):
    __tablename__ = "project_submissions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    project_revision: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    result: Mapped[str] = mapped_column(UnicodeText)
    goal: Mapped[str] = mapped_column(UnicodeText)
    evidence_task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    evidence_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    evidence_sha256: Mapped[str] = mapped_column(String(64))


class ProjectConfirmation(Record, Base):
    __tablename__ = "project_confirmations"
    submission_id: Mapped[str] = mapped_column(ForeignKey("project_submissions.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(UnicodeText)


class Task(Record, Revision, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("status IN ('todo','doing','waiting','review','done')", name="ck_task_status"),
        CheckConstraint("source IN ('project','adhoc','self','assigned')", name="ck_task_source"),
    )
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(Unicode(200))
    description: Mapped[str] = mapped_column(UnicodeText, default="")
    department: Mapped[str] = mapped_column(Unicode(120), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True, index=True)
    phase: Mapped[str] = mapped_column(Unicode(80), default="")
    source: Mapped[str] = mapped_column(String(20), default="self")
    kind: Mapped[str] = mapped_column(Unicode(80), default="一般工作")
    creator_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    proposed_owner_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    assignment: Mapped[str] = mapped_column(String(20), default="self")
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="todo", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=2)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str] = mapped_column(UnicodeText, default="")
    next_action: Mapped[str] = mapped_column(UnicodeText, default="")
    wait_for: Mapped[str] = mapped_column(UnicodeText, default="")
    follow_on: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    target_on: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    planned_start: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    planned_end: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    commitment: Mapped[str] = mapped_column(UnicodeText, default="")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str] = mapped_column(UnicodeText, default="")
    pause_review_on: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cleaned: Mapped[bool] = mapped_column(Boolean, default=False)
    adopted_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_submission_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class TaskAccess(Revision, Base):
    __tablename__ = "task_access"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    confidential: Mapped[bool] = mapped_column(Boolean, default=False)


class AccessReader(Base):
    __tablename__ = "access_readers"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)


class TaskRelation(Base):
    __tablename__ = "task_relations"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CapabilityGrant(Record, Base):
    __tablename__ = "capability_grants"
    __table_args__ = (UniqueConstraint("person_id", "scope_type", "scope_id", "capability", name="uq_capability_grant"),)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    scope_id: Mapped[str] = mapped_column(String(36), index=True)
    scope_type: Mapped[str] = mapped_column(String(20), default="task")
    capability: Mapped[str] = mapped_column(String(30))
    granted_by: Mapped[str] = mapped_column(ForeignKey("people.id"))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Comment(Record, Revision, Base):
    __tablename__ = "comments"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    body: Mapped[str] = mapped_column(UnicodeText)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("comments.id"), nullable=True)
    withdrawn: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Record, Base):
    __tablename__ = "events"
    sequence: Mapped[int] = mapped_column(Integer, unique=True, autoincrement=False)
    resource_type: Mapped[str] = mapped_column(String(30), index=True)
    resource_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    action: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(UnicodeText, default="")
    before_json: Mapped[str] = mapped_column(UnicodeText, default="{}")
    after_json: Mapped[str] = mapped_column(UnicodeText, default="{}")
    visibility: Mapped[str] = mapped_column(String(30), default="read")


class Sequence(Base):
    __tablename__ = "sequences"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Document(Record, Base):
    __tablename__ = "documents"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    title: Mapped[str] = mapped_column(Unicode(255))


class DocumentVersion(Record, Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "number", name="uq_document_version"),)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(Unicode(255))
    blob_key: Mapped[str] = mapped_column(String(64), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str] = mapped_column(String(120))
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    scan_status: Mapped[str] = mapped_column(String(30), default="quarantined")
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class VersionReader(Base):
    __tablename__ = "version_readers"
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), primary_key=True)


class Submission(Record, Base):
    __tablename__ = "submissions"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    result: Mapped[str] = mapped_column(UnicodeText)
    commitment: Mapped[str] = mapped_column(UnicodeText)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    version_sha256: Mapped[str] = mapped_column(String(64))


class Confirmation(Record, Base):
    __tablename__ = "confirmations"
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(UnicodeText)


class RequestKey(Base):
    __tablename__ = "request_keys"
    actor_id: Mapped[str] = mapped_column(ForeignKey("people.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(36))
    operation: Mapped[str] = mapped_column(String(50))
    result_json: Mapped[str] = mapped_column(UnicodeText)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Allocation(Record, Revision, Base):
    __tablename__ = "allocations"
    __table_args__ = (UniqueConstraint("task_id", "person_id", "week", name="uq_allocation"),)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    week: Mapped[datetime] = mapped_column(Date)
    hours: Mapped[float | None] = mapped_column(Numeric(7, 2), nullable=True)
    remaining: Mapped[float | None] = mapped_column(Numeric(7, 2), nullable=True)


class Capacity(Record, Revision, Base):
    __tablename__ = "capacities"
    __table_args__ = (UniqueConstraint("person_id", "week", name="uq_capacity"),)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    week: Mapped[datetime] = mapped_column(Date)
    hours: Mapped[float | None] = mapped_column(Numeric(7, 2), nullable=True)


class Template(Record, Revision, Base):
    __tablename__ = "templates"
    __table_args__ = (Index("uq_template_version", "name", "version", unique=True),)
    name: Mapped[str] = mapped_column(Unicode(120))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    phases_json: Mapped[str] = mapped_column(UnicodeText, default="[]")


class ServiceTerm(Record, Revision, Base):
    __tablename__ = "service_terms"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    basis_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    name: Mapped[str] = mapped_column(Unicode(200))
    scope: Mapped[str] = mapped_column(UnicodeText)
    contact: Mapped[str] = mapped_column(Unicode(255))
    contract_reference: Mapped[str] = mapped_column(Unicode(255))
    checked_status: Mapped[str] = mapped_column(Unicode(80))
    checked_by: Mapped[str] = mapped_column(ForeignKey("people.id"))
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    start_on: Mapped[datetime] = mapped_column(Date)
    end_on: Mapped[datetime] = mapped_column(Date)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class ServiceLog(Record, Base):
    __tablename__ = "service_logs"
    term_id: Mapped[str] = mapped_column(ForeignKey("service_terms.id"))
    basis_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    happened_on: Mapped[datetime] = mapped_column(Date)
    note: Mapped[str] = mapped_column(UnicodeText)


class Coordination(Record, Revision, Base):
    __tablename__ = "coordinations"
    resource_type: Mapped[str] = mapped_column(String(20))
    resource_id: Mapped[str] = mapped_column(String(36), index=True)
    creator_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    subject: Mapped[str] = mapped_column(UnicodeText)
    follow_on: Mapped[datetime] = mapped_column(Date)
    resolution: Mapped[str] = mapped_column(UnicodeText, default="")


class CleanupItem(Record, Revision, Base):
    __tablename__ = "cleanup_items"
    __table_args__ = (Index("uq_cleanup_resource", "resource_type", "resource_id", unique=True),)
    resource_type: Mapped[str] = mapped_column(String(30))
    resource_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    deferred_until: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str] = mapped_column(UnicodeText, default="")
    failure_code: Mapped[str] = mapped_column(String(80), default="")


class CleanupHistory(Record, Base):
    __tablename__ = 'cleanup_history'
    resource_type: Mapped[str] = mapped_column(String(30))
    resource_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str | None] = mapped_column(ForeignKey('people.id'), nullable=True)
    action: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(UnicodeText)
    status: Mapped[str] = mapped_column(String(30))
