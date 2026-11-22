import json
from datetime import datetime, timedelta

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import and_, delete, exists, func, or_, select

from .auth import authenticated
from .errors import DomainError, require
from .models import (AccessReader, CapabilityGrant, CleanupItem, Coordination, DocumentVersion, Person, Project, ProjectMember, TaskAccess,
                     ServiceLog, ServiceTerm, Task, Template, utcnow)
from .policy import (can_comment, can_edit, can_finalize, can_manage, can_plan_project,
                     can_read, can_read_project, can_read_version, can_read_version_record, global_capability, has_capability,
                     readable_task, task_read_clause, work_eligible)
from .services import audit, children_closed, day, expected, fields, person, text
from .storage import read_blob

governance = Blueprint('governance', __name__, url_prefix='/api')
PHASES = ['需求與RFP', 'POC', '報價評估', '簽呈核對', '議價', '簽約下單', '啟動準備', '建置測試',
          '驗收與缺失改善', '維運交接', '付款追蹤與結案', '保固保養', '到期與續約']


@governance.get('/maintenance/status')
@authenticated
def maintenance_status():
    require(global_capability(g.user, 'backup'), '無權檢視維運狀態。', 403)
    from .maintenance import status_snapshot
    return jsonify(status_snapshot())


@governance.get('/maintenance/runbook.md')
@authenticated
def maintenance_runbook():
    require(global_capability(g.user, 'backup'), '無權取得維運手冊。', 403)
    from pathlib import Path
    from flask import send_file
    source = Path(__file__).resolve().parents[2] / 'docs' / 'OPERATIONS_TEST.md'
    require(source.is_file(), '維運手冊暫時不可用。', 503)
    return send_file(source, mimetype='text/markdown', as_attachment=True,
                     download_name='workboard-operations.md', etag=False, conditional=False)


def data_body():
    data = request.get_json(silent=True)
    require(isinstance(data, dict), '請提供 JSON 物件。')
    return data


def template_payload(row):
    return {'id': row.id, 'name': row.name, 'version': row.version, 'status': row.status,
            'requires_confirmation': row.requires_confirmation, 'phases': json.loads(row.phases_json), 'revision': row.revision}


@governance.get('/templates')
@authenticated
def templates():
    require(work_eligible(g.user) or global_capability(g.user, 'templates'), '無權查看範本。', 403)
    query = select(Template).order_by(Template.name, Template.version.desc())
    if not global_capability(g.user, 'templates'):
        query = query.where(Template.status == 'active')
    return jsonify({'items': [template_payload(t) for t in g.db.scalars(query)]})


@governance.post('/templates')
@authenticated
def create_template():
    require(global_capability(g.user, 'templates'), '無權維護範本。', 403)
    data = data_body()
    fields(data, {'name', 'phases', 'requires_confirmation'})
    name = text(data, 'name', 120, True)
    phases = data.get('phases', [])
    require(isinstance(phases, list) and 0 < len(phases) <= 50 and all(isinstance(p, str) and 0 < len(p.strip()) <= 80 for p in phases), '階段格式不符。')
    require(len({p.strip() for p in phases}) == len(phases), '階段不可重複。')
    phases = [p.strip() for p in phases]
    require(type(data.get('requires_confirmation', False)) is bool, '確認條件格式不符。')
    version = (g.db.scalar(select(func.max(Template.version)).where(Template.name == name)) or 0) + 1
    row = Template(name=name, version=version, phases_json=json.dumps(phases, ensure_ascii=False),
                   requires_confirmation=data.get('requires_confirmation', False), status='draft')
    g.db.add(row)
    g.db.flush()
    return jsonify({'template': template_payload(row)})


@governance.patch('/templates/<template_id>')
@authenticated
def edit_template(template_id):
    require(global_capability(g.user, 'templates'), '無權維護範本。', 403)
    data = data_body()
    fields(data, {'revision', 'status', 'reason'})
    row = g.db.get(Template, template_id)
    require(row is not None, '找不到範本。', 404)
    expected(row, data)
    text(data, 'reason', required=True)
    require(data.get('status') in {'active', 'retired'}, '範本狀態不符。')
    require(row.status != 'retired' or data['status'] == 'retired', '已停用版本不可重新發布，請建立新版。', 409)
    row.status = data['status']
    g.db.flush()
    return jsonify({'template': template_payload(row)})


def scope_resource(kind, resource_id):
    require(kind in {'task', 'project'}, '授權範圍不符。')
    if kind == 'task':
        return readable_task(g.db, g.user, resource_id)
    from .projects import readable
    return readable(g.db, g.user, resource_id)


def can_grant(row):
    if isinstance(row, Task):
        return can_manage(g.db, g.user, row, 'grant')
    from .projects import capability
    return capability(g.db, g.user, row, 'grant')


@governance.get('/grants/<kind>/<resource_id>')
@authenticated
def grants(kind, resource_id):
    row = scope_resource(kind, resource_id)
    require(can_grant(row), '無權管理此範圍能力。', 403)
    return jsonify({'items': [{'id': grant.id, 'person_id': grant.person_id, 'capability': grant.capability,
                               'valid_until': grant.valid_until.isoformat() + 'Z' if grant.valid_until else None}
                              for grant in g.db.scalars(select(CapabilityGrant).where(CapabilityGrant.scope_type == kind, CapabilityGrant.scope_id == row.id))]})


@governance.post('/grants/<kind>/<resource_id>')
@authenticated
def grant(kind, resource_id):
    row = scope_resource(kind, resource_id)
    require(can_grant(row), '無權管理此範圍能力。', 403)
    data = data_body()
    fields(data, {'person_id', 'capability', 'valid_until', 'reason', 'revoke'})
    target = person(g.db, g.user, data.get('person_id'))
    capability = data.get('capability')
    require(capability in {'dispatch', 'priority', 'confirm', 'commit', 'cancel', 'pause', 'share', 'confidential', 'grant', 'comment'}, '能力不符。')
    require(target.id != g.user.id, '不可授予或撤銷本人的操作權限。', 403)
    require(capability != 'grant' or global_capability(g.user, 'grant'), '轉授權不可再轉授。', 403)
    reason = text(data, 'reason', required=True)
    previous = g.db.scalar(select(CapabilityGrant).where(CapabilityGrant.person_id == target.id, CapabilityGrant.scope_type == kind,
                                                        CapabilityGrant.scope_id == row.id, CapabilityGrant.capability == capability))
    before = {'person_id': target.id, 'capability': capability, 'valid_until': previous.valid_until.isoformat() if previous and previous.valid_until else None, 'existed': previous is not None}
    require(type(data.get('revoke', False)) is bool, '撤銷選項格式不符。')
    if data.get('revoke') is True:
        if previous:
            g.db.delete(previous)
            g.db.flush()
            if capability == 'confidential':
                require_confidential_steward(kind, row, utcnow())
    else:
        if previous:
            cleaned = g.db.scalar(select(CleanupItem).where(CleanupItem.resource_type == 'grant',
                CleanupItem.resource_id == previous.id, CleanupItem.status == 'done'))
            require(cleaned is None, '此授權紀錄已清理，請先撤銷舊項目再建立新授權。', 409)
        valid_until = None
        if data.get('valid_until'):
            limit = day(data['valid_until'])
            require(limit >= (utcnow() + timedelta(hours=8)).date(), '授權到期日不可早於今日。')
            valid_until = datetime.combine(limit + timedelta(days=1), datetime.min.time()) - timedelta(hours=8)
        if previous:
            previous.valid_until = valid_until
        else:
            g.db.add(CapabilityGrant(person_id=target.id, scope_type=kind, scope_id=row.id, capability=capability,
                                     granted_by=g.user.id, valid_until=valid_until))
        if capability == 'confidential' and valid_until:
            g.db.flush()
            require_confidential_steward(kind, row, valid_until)
    audit(g.db, g.user, row, 'grant', reason, before, after={'scope_type': kind, 'person_id': target.id, 'capability': capability,
                                                  'revoked': data.get('revoke') is True}, visibility='access')
    return jsonify({'ok': True})


def require_confidential_steward(kind, row, at):
    query = select(Task).join(TaskAccess, TaskAccess.task_id == Task.id).where(TaskAccess.confidential.is_(True))
    query = query.where(Task.id == row.id) if kind == 'task' else query.where(Task.project_id == row.id)
    for task in g.db.scalars(query):
        readers = g.db.scalars(select(Person).join(AccessReader, AccessReader.person_id == Person.id).where(
            AccessReader.task_id == task.id, AccessReader.kind == 'confidential'))
        def eligible(person):
            if not work_eligible(person) or person.department != task.department:
                return False
            if global_capability(person, 'confidential'):
                return True
            return bool(g.db.scalar(select(exists().where(
                CapabilityGrant.person_id == person.id, CapabilityGrant.capability == 'confidential',
                or_(and_(CapabilityGrant.scope_type == 'task', CapabilityGrant.scope_id == task.id),
                    and_(CapabilityGrant.scope_type == 'project', CapabilityGrant.scope_id == task.project_id)),
                # A chain of finite grants can all expire after this check.
                # Keep one continuing steward when shortening or revoking.
                CapabilityGrant.valid_until.is_(None)))))
        require(any(eligible(person) for person in readers), '須先安排有效的機密名單管理者。', 409)


def term_payload(row):
    return {key: getattr(row, key) for key in ['id', 'task_id', 'basis_version_id', 'name', 'scope', 'contact',
            'contract_reference', 'checked_status', 'checked_by', 'cancelled', 'revision']} | {
            'start_on': row.start_on.isoformat(), 'end_on': row.end_on.isoformat(), 'checked_at': row.checked_at.isoformat() + 'Z', 'basis_purged': bool(g.db.get(DocumentVersion, row.basis_version_id).deleted)}


@governance.get('/service-terms')
@authenticated
def terms():
    tasks = select(Task.id).where(task_read_clause(g.user))
    rows = g.db.scalars(select(ServiceTerm).where(ServiceTerm.task_id.in_(tasks)).order_by(ServiceTerm.end_on))
    return jsonify({'items': [term_payload(r) for r in rows
                              if can_read_version_record(g.db, g.user, g.db.get(Task, r.task_id), g.db.get(DocumentVersion, r.basis_version_id))]})


@governance.post('/service-terms')
@authenticated
def create_term():
    data = data_body()
    fields(data, {'task_id', 'basis_version_id', 'name', 'scope', 'contact', 'contract_reference', 'checked_status', 'start_on', 'end_on', 'reason'})
    task = readable_task(g.db, g.user, data.get('task_id'))
    require(can_finalize(g.db, g.user, task) or can_manage(g.db, g.user, task, 'commit'), '無權管理服務條件。', 403)
    version = g.db.get(DocumentVersion, data.get('basis_version_id'))
    require(can_read_version(g.db, g.user, task, version), '找不到合約依據版本。', 404)
    read_blob(version)
    start, end = day(data.get('start_on')), day(data.get('end_on'))
    require(start and end and start <= end, '合約起訖日期不符。')
    row = ServiceTerm(task_id=task.id, basis_version_id=version.id, checked_by=g.user.id, start_on=start, end_on=end,
                      **{key: text(data, key, 200 if key == 'name' else 255 if key in {'contact', 'contract_reference'} else 80 if key == 'checked_status' else 8000, True)
                         for key in ['name', 'scope', 'contact', 'contract_reference', 'checked_status']})
    g.db.add(row)
    g.db.flush()
    audit(g.db, g.user, task, 'service_term', text(data, 'reason', required=True), after={'term_id': row.id}, visibility='service')
    return jsonify({'term': term_payload(row)})


@governance.patch('/service-terms/<term_id>')
@authenticated
def edit_term(term_id):
    row = g.db.get(ServiceTerm, term_id)
    require(row is not None, '找不到服務條件。', 404)
    task = readable_task(g.db, g.user, row.task_id)
    require(can_finalize(g.db, g.user, task) or can_manage(g.db, g.user, task, 'commit'), '無權管理服務條件。', 403)
    require(can_read_version_record(g.db, g.user, task, g.db.get(DocumentVersion, row.basis_version_id)), '找不到服務條件。', 404)
    data = data_body()
    fields(data, {'revision', 'name', 'scope', 'contact', 'contract_reference', 'checked_status', 'start_on', 'end_on', 'reason', 'cancelled'})
    expected(row, data)
    before = term_payload(row)
    for key in {'name', 'scope', 'contact', 'contract_reference', 'checked_status'} & set(data):
        setattr(row, key, text(data, key, 200 if key == 'name' else 255 if key in {'contact', 'contract_reference'} else 80 if key == 'checked_status' else 8000, True))
    for key in {'start_on', 'end_on'} & set(data):
        setattr(row, key, day(data[key]))
    require(row.start_on and row.end_on and row.start_on <= row.end_on, '合約日期不符。')
    if 'cancelled' in data:
        require(type(data['cancelled']) is bool, '取消狀態不符。')
        row.cancelled = data['cancelled']
    row.checked_by, row.checked_at = g.user.id, utcnow()
    audit(g.db, g.user, task, 'service_term', text(data, 'reason', required=True), before, term_payload(row), visibility='service')
    g.db.flush()
    return jsonify({'term': term_payload(row)})


@governance.route('/service-terms/<term_id>/logs', methods=['GET', 'POST'])
@authenticated
def term_logs(term_id):
    row = g.db.get(ServiceTerm, term_id)
    require(row is not None, '找不到服務條件。', 404)
    task = readable_task(g.db, g.user, row.task_id)
    require(can_read_version_record(g.db, g.user, task, g.db.get(DocumentVersion, row.basis_version_id)), '找不到服務條件。', 404)
    if request.method == 'POST':
        require(can_edit(g.db, g.user, task), '無權新增維運紀錄。', 403)
        data = data_body()
        fields(data, {'happened_on', 'note'})
        happened = day(data.get('happened_on'))
        require(happened, '請填寫發生日。')
        g.db.add(ServiceLog(term_id=row.id, basis_version_id=row.basis_version_id, author_id=g.user.id,
                           happened_on=happened, note=text(data, 'note', required=True)))
        g.db.flush()
    return jsonify({'items': [{'id': r.id, 'note': r.note, 'author_id': r.author_id, 'happened_on': r.happened_on.isoformat()}
                              for r in g.db.scalars(select(ServiceLog).where(ServiceLog.term_id == row.id).order_by(ServiceLog.happened_on))]})


def coordination_target(row):
    if row.resource_type == 'task':
        return g.db.get(Task, row.resource_id)
    return g.db.get(Project, row.resource_id)


def target_read(actor, target):
    return can_read(g.db, actor, target) if isinstance(target, Task) else can_read_project(g.db, actor, target)


def target_responsible(actor, target):
    return (can_edit(g.db, actor, target) or can_manage(g.db, actor, target, 'dispatch')) if isinstance(target, Task) else can_plan_project(g.db, actor, target)


def coordination_payload(row):
    return {key: getattr(row, key) for key in ['id', 'resource_type', 'resource_id', 'creator_id', 'assignee_id', 'status', 'subject', 'resolution', 'revision']} | {'follow_on': row.follow_on.isoformat()}


@governance.get('/coordinations')
@authenticated
def coordinations():
    rows = g.db.scalars(select(Coordination).where((Coordination.creator_id == g.user.id) | (Coordination.assignee_id == g.user.id)).order_by(Coordination.follow_on))
    return jsonify({'items': [coordination_payload(r) for r in rows if target_read(g.user, coordination_target(r))]})


@governance.post('/coordinations')
@authenticated
def create_coordination():
    data = data_body()
    fields(data, {'resource_type', 'resource_id', 'assignee_id', 'subject', 'follow_on'})
    target = scope_resource(data.get('resource_type'), data.get('resource_id'))
    require(target_responsible(g.user, target), '無權提出此協調。', 403)
    assignee = person(g.db, g.user, data.get('assignee_id'))
    require(assignee.id != g.user.id and target_read(assignee, target), '受理人須為另一位已獲授權的人員。', 403)
    follow = day(data.get('follow_on'))
    require(follow, '請填寫追蹤日。')
    duplicate = g.db.scalar(select(Coordination).where(Coordination.resource_type == data['resource_type'], Coordination.resource_id == target.id,
                                                       Coordination.creator_id == g.user.id, Coordination.status.in_(['pending', 'accepted'])))
    require(duplicate is None, '已有未結協調，請更新原申請。', 409)
    row = Coordination(resource_type=data['resource_type'], resource_id=target.id, creator_id=g.user.id,
                        assignee_id=assignee.id, subject=text(data, 'subject', required=True), follow_on=follow)
    g.db.add(row)
    g.db.flush()
    audit(g.db, g.user, target, 'coordination', after={'coordination_id': row.id}, visibility='coordination')
    return jsonify({'coordination': coordination_payload(row)})


@governance.post('/coordinations/<coordination_id>/actions')
@authenticated
def act_coordination(coordination_id):
    row = g.db.get(Coordination, coordination_id)
    target = coordination_target(row) if row else None
    require(row is not None and g.user.id in {row.creator_id, row.assignee_id} and target_read(g.user, target), '找不到協調。', 404)
    data = data_body()
    fields(data, {'revision', 'action', 'reason', 'assignee_id', 'follow_on'})
    expected(row, data)
    require(row.status in {'pending', 'accepted', 'restore_review'}, '此協調已結束。', 409)
    if row.status == 'restore_review':
        require(g.user.id == row.creator_id and data.get('action') in {'withdraw', 'reassign'}, '還原後須由提出人重新指派或撤回。', 409)
    before = coordination_payload(row)
    action = data.get('action')
    reason = text(data, 'reason', required=True)
    if action == 'withdraw':
        require(g.user.id == row.creator_id, '僅提出人可撤回。', 403)
        row.status = 'withdrawn'
    elif action == 'reassign':
        require(g.user.id == row.creator_id, '僅提出人可改派。', 403)
        assignee = person(g.db, g.user, data.get('assignee_id'))
        require(assignee.id != row.creator_id and target_read(assignee, target), '受理人資格不符。', 403)
        row.assignee_id, row.status = assignee.id, 'pending'
    elif action in {'accept', 'complete', 'forward'}:
        require(g.user.id == row.assignee_id, '僅受理人可處理。', 403)
        if action == 'complete':
            require(row.status == 'accepted', '請先受理。', 409)
            if isinstance(target, Task):
                children_closed(g.db, target)
            else:
                from .projects import all_closed
                all_closed(g.db, target)
        row.status = {'accept': 'accepted', 'complete': 'completed', 'forward': 'forwarded'}[action]
    elif action != 'update':
        raise DomainError('協調操作不符。')
    row.resolution = reason
    if data.get('follow_on'):
        row.follow_on = day(data['follow_on'])
    audit(g.db, g.user, target, 'coordination', reason, before, {**coordination_payload(row), 'coordination_id': row.id}, visibility='coordination')
    g.db.flush()
    return jsonify({'coordination': coordination_payload(row)})


@governance.patch('/projects/<project_id>/members')
@authenticated
def project_members(project_id):
    from .projects import readable, capability, payload
    row = readable(g.db, g.user, project_id)
    require(row.owner_id == g.user.id or capability(g.db, g.user, row, 'dispatch'), '無權調整規劃人員。', 403)
    require(not row.cleaned, '專案內容已到期清理。', 409)
    data = data_body()
    fields(data, {'revision', 'members', 'reason'})
    expected(row, data)
    ids = data.get('members')
    require(isinstance(ids, list) and len(ids) <= 100 and all(isinstance(pid, str) for pid in ids), '規劃名單格式不符。')
    for pid in set(ids):
        person(g.db, g.user, pid)
    before = list(g.db.scalars(select(ProjectMember.person_id).where(ProjectMember.project_id == row.id)))
    g.db.execute(delete(ProjectMember).where(ProjectMember.project_id == row.id))
    g.db.add_all([ProjectMember(project_id=row.id, person_id=pid) for pid in set(ids)])
    row.revision += 1
    audit(g.db, g.user, row, 'members', text(data, 'reason', required=True), {'members': before}, {'members': ids})
    g.db.flush()
    return jsonify({'project': payload(g.db, g.user, row)})
