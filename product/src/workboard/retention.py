"""Per-record expiry, selected-only cleanup, resumable physical removal.

Tombstone identifiers preserve unselected references. Content is cleared only
after that record's own expiry. Files are removed AFTER the clearing transaction
commits, so database rollback cannot turn a live version into a missing file.
"""
from datetime import date, timedelta

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError

from .auth import authenticated
from .errors import DomainError, require
from .models import (Allocation, CapabilityGrant, Capacity, CleanupHistory, CleanupItem, Comment, Confirmation, Coordination,
                     Document, DocumentVersion, Event, Project, ProjectConfirmation,
                     ProjectSubmission, SecurityEvent, ServiceLog, ServiceTerm,
                     Submission, Task, Template, utcnow)
from .policy import global_capability
from .services import day, fields, text
from .storage import blob_path

retention = Blueprint('retention', __name__, url_prefix='/api')
TARGETS = {'task': Task, 'project': Project, 'comment': Comment, 'document': Document, 'version': DocumentVersion,
           'submission': Submission, 'confirmation': Confirmation, 'project_submission': ProjectSubmission,
           'project_confirmation': ProjectConfirmation, 'event': Event, 'allocation': Allocation,
           'coordination': Coordination, 'service_term': ServiceTerm, 'service_log': ServiceLog,
           'template': Template, 'security_event': SecurityEvent, 'cleanup_history': CleanupHistory,
           'capacity': Capacity, 'cleanup_state': CleanupItem, 'grant': CapabilityGrant}


def cleanup_permission():
    require(global_capability(g.user, 'cleanup'), '無權清理到期資料。', 403)


def state(db, kind, resource_id):
    return db.scalar(select(CleanupItem).where(CleanupItem.resource_type == kind, CleanupItem.resource_id == resource_id))


def active_impacts(record):
    # These flags contain no titles, parties, contract details or file names.
    if isinstance(record, ServiceTerm) and not record.cancelled and record.end_on >= utcnow().date():
        return ['active_service']
    if isinstance(record, Template) and record.status == 'active':
        return ['active_template']
    if isinstance(record, Task) and not record.cancelled and not record.deleted and not record.cleaned and record.status != 'done':
        return ['active_task']
    if isinstance(record, Project) and not record.cancelled and not record.cleaned and record.status != 'done':
        return ['active_project']
    if isinstance(record, Coordination) and record.status in {'pending', 'accepted', 'restore_review'}:
        return ['open_coordination']
    if isinstance(record, CapabilityGrant) and (record.valid_until is None or record.valid_until > utcnow()):
        return ['active_grant']
    if isinstance(record, (Allocation, Capacity)):
        today = (utcnow() + timedelta(hours=8)).date()
        if record.week >= today - timedelta(days=today.weekday()):
            return ['future_allocation']
    if isinstance(record, DocumentVersion):
        impacts = []
        if g.db.scalar(select(exists().where(ServiceTerm.basis_version_id == record.id,
                        ServiceTerm.cancelled.is_(False), ServiceTerm.end_on >= utcnow().date()))):
            impacts.append('contract_basis')
        if any(g.db.scalar(select(exists().where(column == record.id))) for column in
               [Task.adopted_version_id, Submission.version_id, ProjectSubmission.evidence_version_id]):
            impacts.append('adopted_evidence')
        return impacts
    return []


def metadata(kind, record, item):
    # Never include titles, filenames, comments, people or confidential content.
    return {'resource_type': kind, 'resource_id': record.id, 'created_at': record.created_at.isoformat() + 'Z',
            'expires_at': record.expires_at.isoformat() + 'Z', 'revision': getattr(record, 'revision', 0),
            'status': item.status if item else 'pending',
            'deferred_until': item.deferred_until.isoformat() if item and item.deferred_until else None,
            'failure_code': item.failure_code if item else '', 'impacts': active_impacts(record),
            'reference': getattr(record, 'task_id', None) or getattr(record, 'project_id', None)}


@retention.get('/cleanup')
@authenticated
def candidates():
    cleanup_permission()
    items = []
    now = utcnow()
    for kind, model in TARGETS.items():
        for record in g.db.scalars(select(model).where(model.expires_at <= now).order_by(model.expires_at)):
            item = state(g.db, kind, record.id)
            if item and item.status == 'done':
                continue
            items.append(metadata(kind, record, item))
    return jsonify({'items': items, 'as_of': now.isoformat() + 'Z'})


def clear_content(record):
    if isinstance(record, Task):
        record.title, record.description, record.result, record.next_action = '已清理工作', '', '', ''
        record.wait_for, record.commitment, record.pause_reason, record.cleaned = '', '', '', True
    elif isinstance(record, Project):
        record.name, record.goal, record.next_action, record.wait_for = '已清理專案', '', '', ''
        record.template_snapshot, record.cleaned = '{}', True
    elif isinstance(record, Comment):
        record.body, record.withdrawn = '', True
    elif isinstance(record, DocumentVersion):
        record.filename, record.deleted, record.scan_status = '已清理附件', True, 'deleted'
    elif isinstance(record, Document):
        record.title = '已清理附件'
    elif isinstance(record, Submission):
        record.result, record.commitment = '已清理成果摘要', ''
    elif isinstance(record, ProjectSubmission):
        record.result, record.goal = '已清理成果摘要', ''
    elif isinstance(record, (Confirmation, ProjectConfirmation)):
        record.reason = '已清理確認意見'
    elif isinstance(record, Event):
        record.reason, record.before_json, record.after_json = '', '{}', '{}'
    elif isinstance(record, Allocation):
        record.hours, record.remaining = None, None
    elif isinstance(record, Capacity):
        record.hours = None
    elif isinstance(record, CapabilityGrant):
        record.valid_until = utcnow() - timedelta(seconds=1)
    elif isinstance(record, Coordination):
        record.subject, record.resolution, record.status = '已清理協調', '', 'expired'
    elif isinstance(record, ServiceTerm):
        record.name, record.scope, record.contact, record.contract_reference = '已清理服務條件', '', '', ''
        record.checked_status, record.cancelled = '', True
    elif isinstance(record, ServiceLog):
        record.note = '已清理維運紀錄'
    elif isinstance(record, Template):
        record.name, record.phases_json, record.status = '已清理範本-' + record.id, '[]', 'retired'
    elif isinstance(record, SecurityEvent):
        record.actor_id, record.address_hash, record.route, record.code = None, '', '', 'expired'
    elif isinstance(record, CleanupHistory):
        record.actor_id, record.reason = None, ''
    elif isinstance(record, CleanupItem):
        # Keep only the operational state required to avoid reviving content.
        record.reason = ''


def remove_committed_blob(item_id):
    sessions = current_app.extensions['sessions']
    with sessions.begin() as db:
        item = db.get(CleanupItem, item_id)
        require(item is not None and item.resource_type == 'version' and item.status in {'pending_file', 'failed_file'}, '清理狀態不符。', 409)
        version = db.get(DocumentVersion, item.resource_id)
        require(version is not None and version.deleted and version.expires_at <= utcnow(), '附件尚未到期或清理未提交。', 409)
        try:
            path = blob_path(version.blob_key)
            # Only the immutable private blob resolved by its DB key is touched.
            path.unlink(missing_ok=True)
            item.status, item.failure_code = 'done', ''
        except Exception:
            item.status, item.failure_code = 'failed_file', 'attachment_remove_failed'
        db.add(CleanupHistory(resource_type='version', resource_id=version.id, actor_id=None,
                              action='file_remove', reason='執行已確認的附件移除', status=item.status))
        return {'resource_type': 'version', 'resource_id': version.id, 'status': item.status, 'failure_code': item.failure_code}


@retention.post('/cleanup')
@authenticated
def cleanup_batch():
    cleanup_permission()
    data = request.get_json(silent=True)
    fields(data, {'action', 'items', 'reason', 'deferred_until', 'confirmed'})
    require(data.get('action') in {'clean', 'defer', 'resume'}, '清理操作不符。')
    require(data.get('confirmed') is True, '請確認本次選取範圍。')
    selected = data.get('items')
    require(isinstance(selected, list) and 0 < len(selected) <= 100, '每批請選取 1 至 100 筆。')
    reason = text(data, 'reason', 2000, True)
    deferred = day(data.get('deferred_until')) if data['action'] == 'defer' else None
    require(data['action'] != 'defer' or deferred and deferred > utcnow().date(), '暫緩日期須晚於今日。')
    results, seen, pending = [], set(), []
    for selection in selected:
        try:
            fields(selection, {'resource_type', 'resource_id', 'revision', 'acknowledge_impacts'})
            kind, resource_id = selection.get('resource_type'), selection.get('resource_id')
            require(isinstance(kind, str) and kind in TARGETS and isinstance(resource_id, str), '清理項目不符。')
            require((kind, resource_id) not in seen, '不可重複選取。')
            seen.add((kind, resource_id))
            with g.db.begin_nested():
                record = g.db.get(TARGETS[kind], resource_id)
                require(record is not None and record.expires_at <= utcnow(), '紀錄尚未到期或不存在。', 409)
                require(selection.get('revision') == getattr(record, 'revision', 0), '紀錄已更新，請重新核對。', 409)
                item = state(g.db, kind, resource_id)
                if item is None:
                    item = CleanupItem(resource_type=kind, resource_id=resource_id)
                    g.db.add(item)
                if item.status == 'done':
                    results.append({'resource_type': kind, 'resource_id': resource_id, 'status': 'done'})
                    continue
                item.reason, item.failure_code = reason, ''
                if data['action'] == 'resume':
                    require(item.status == 'deferred', '此項目未暫緩。', 409)
                    item.status, item.deferred_until = 'pending', None
                elif data['action'] == 'defer':
                    require(item.status not in {'pending_file', 'failed_file'}, '內容已清理，請完成檔案移除。', 409)
                    item.status, item.deferred_until = 'deferred', deferred
                else:
                    require(item.status != 'deferred' or item.deferred_until is None or item.deferred_until <= utcnow().date(), '尚在暫緩期間，請先解除暫緩並記錄原因。', 409)
                    require(not active_impacts(record) or selection.get('acknowledge_impacts') is True, '此紀錄仍在使用，請核對影響並明確確認。', 409, 'active_record_acknowledgement_required')
                    clear_content(record)
                    if isinstance(record, CapabilityGrant) and record.capability == 'confidential':
                        from .governance import require_confidential_steward
                        resource = g.db.get(Task if record.scope_type == 'task' else Project, record.scope_id)
                        if resource:
                            g.db.flush()
                            require_confidential_steward(record.scope_type, resource, utcnow())
                    item.status = 'pending_file' if kind == 'version' else 'done'
                g.db.flush()
                g.db.add(CleanupHistory(resource_type=kind, resource_id=resource_id, actor_id=g.user.id,
                                        action=data['action'], reason=reason, status=item.status))
                g.db.flush()
            if item.status == 'pending_file':
                pending.append(item.id)
            results.append({'resource_type': kind, 'resource_id': resource_id, 'status': item.status})
        except (DomainError, IntegrityError) as error:
            if isinstance(selection, dict) and isinstance(selection.get('resource_type'), str) and selection.get('resource_type') in TARGETS and isinstance(selection.get('resource_id'), str) and len(selection['resource_id']) <= 36:
                g.db.add(CleanupHistory(resource_type=selection['resource_type'], resource_id=selection['resource_id'], actor_id=g.user.id,
                                        action=data['action'], reason=reason, status='failed'))
            results.append({'resource_type': selection.get('resource_type') if isinstance(selection, dict) else None,
                            'resource_id': selection.get('resource_id') if isinstance(selection, dict) else None,
                            'status': 'failed', 'failure_code': error.code if isinstance(error, DomainError) else 'conflict',
                            'error': error.message if isinstance(error, DomainError) else '此紀錄發生資料衝突，請重新核對。'})
    g.cleanup_pending = pending
    return jsonify({'results': results})


@retention.get('/cleanup/history')
@authenticated
def cleanup_history():
    cleanup_permission()
    return jsonify({'items': [{'id': r.id, 'resource_type': r.resource_type, 'resource_id': r.resource_id,
                               'actor_id': r.actor_id, 'action': r.action, 'reason': r.reason, 'status': r.status,
                               'at': r.created_at.isoformat() + 'Z'} for r in g.db.scalars(select(CleanupHistory).order_by(CleanupHistory.created_at.desc()).limit(500))]})
