"""Project decomposition; visible rollups and complete closure guards differ."""
import json
from sqlalchemy import exists, or_, select
from .errors import DomainError, require
from .models import (CapabilityGrant, DocumentVersion, Project, ProjectConfirmation,
                     ProjectMember, ProjectSubmission, Task, Template, utcnow)
from .policy import (can_plan_project, can_read_project, can_read_version,
                     global_capability, readable_task, task_read_clause, work_eligible)
from .services import audit, day, expected, fields, fixed_code, person, text
from .storage import read_blob


def readable(db, actor, project_id):
    row = db.get(Project, project_id)
    require(can_read_project(db, actor, row), '找不到專案。', 404)
    return row


def capability(db, actor, project, name):
    return can_read_project(db, actor, project) and (global_capability(actor, name) or db.scalar(select(exists().where(
        CapabilityGrant.person_id == actor.id, CapabilityGrant.scope_type == 'project', CapabilityGrant.scope_id == project.id, CapabilityGrant.capability == name,
        or_(CapabilityGrant.valid_until.is_(None), CapabilityGrant.valid_until > utcnow())))))


def ancestors_open(db, project):
    seen = {project.id}
    parent_id = project.parent_id
    while parent_id:
        require(parent_id not in seen and len(seen) <= 32, '專案階層異常。', 409)
        seen.add(parent_id)
        parent = db.get(Project, parent_id)
        require(parent is not None and not parent.cleaned and not parent.cancelled and parent.status not in {'review', 'done'}, '請先恢復上層專案。', 409)
        parent_id = parent.parent_id


def subtree(db, project):
    ids, pending = set(), [project.id]
    while pending:
        current = pending.pop()
        require(current not in ids, '專案階層異常。', 409)
        ids.add(current)
        pending.extend(db.scalars(select(Project.id).where(Project.parent_id == current)))
    return ids


def all_closed(db, project):
    ids = subtree(db, project)
    require(not db.scalar(select(exists().where(Project.id.in_(ids - {project.id}), Project.cancelled.is_(False), Project.status != 'done'))), '仍有未結事項，請洽協調主責核對。', 409)
    require(not db.scalar(select(exists().where(Task.project_id.in_(ids), Task.deleted.is_(False), Task.cancelled.is_(False), Task.status != 'done'))), '仍有未結事項，請洽協調主責核對。', 409)
    return ids


def payload(db, actor, project):
    tasks = list(db.scalars(select(Task).where(Task.project_id.in_(subtree(db, project)), task_read_clause(actor))))
    active = [t for t in tasks if not t.cancelled]
    from .planning import visible_leaf_dates
    roots = list(db.scalars(select(Task).where(Task.project_id.in_(subtree(db, project)), Task.parent_id.is_(None), Task.deleted.is_(False))))
    planned_start, planned_end = visible_leaf_dates(db, actor, roots)
    parent = db.get(Project, project.parent_id) if project.parent_id else None
    return {'id': project.id, 'code': project.code, 'name': project.name, 'goal': project.goal,
            'owner_id': project.owner_id, 'parent_id': parent.id if parent and can_read_project(db, actor, parent) else None,
            'status': project.status, 'revision': project.revision, 'cancelled': project.cancelled, 'cleaned': project.cleaned,
            'next_action': project.next_action, 'wait_for': project.wait_for,
            'follow_on': project.follow_on.isoformat() if project.follow_on else None,
            'current_submission_id': project.current_submission_id,
            'visible_total': len(active), 'visible_done': sum(t.status == 'done' for t in active),
            'planned_start': planned_start.isoformat() if planned_start else None,
            'planned_end': planned_end.isoformat() if planned_end else None,
            'template': json.loads(project.template_snapshot),
            'members': list(db.scalars(select(ProjectMember.person_id).where(ProjectMember.project_id == project.id))),
            'scope': '目前授權範圍', 'permissions': {'edit': can_plan_project(db, actor, project),
            'dispatch': can_plan_project(db, actor, project) and capability(db, actor, project, 'dispatch'),
            'submit': actor.id == project.owner_id, 'confirm': capability(db, actor, project, 'confirm'),
            'members': project.owner_id == actor.id or capability(db, actor, project, 'dispatch'),
            'grant': capability(db, actor, project, 'grant'), 'cancel': capability(db, actor, project, 'cancel')}}


def create(db, actor, data):
    fields(data, {'name', 'goal', 'owner_id', 'parent_id', 'members', 'template_id', 'skipped_phases'})
    require(work_eligible(actor), '無權建立專案。', 403)
    parent_id = data.get('parent_id')
    if parent_id:
        parent = readable(db, actor, parent_id)
        require(can_plan_project(db, actor, parent), '無權分解專案。', 403)
        require(not parent.cleaned and not parent.cancelled and parent.status not in {'review', 'done'}, '請先恢復上層專案。', 409)
        ancestors_open(db, parent)
    owner = person(db, actor, data.get('owner_id') or actor.id)
    require(owner.id == actor.id or global_capability(actor, 'dispatch'), '無權指定專案主責。', 403)
    row = Project(code=fixed_code(db, 'proj'), name=text(data, 'name', 200, True), goal=text(data, 'goal', required=True),
                  owner_id=owner.id, parent_id=parent_id, department=actor.department)
    template = None
    skipped = data.get('skipped_phases', {})
    require(isinstance(skipped, dict), '略過階段格式不符。')
    require(not skipped or data.get('template_id'), '略過階段須指定範本。')
    if data.get('template_id'):
        template = db.get(Template, data['template_id'])
        require(template is not None and template.status == 'active', '範本不可使用。')
        phase_names = json.loads(template.phases_json)
        require(all(k in phase_names and isinstance(v, str) and 0 < len(v.strip()) <= 2000 for k, v in skipped.items()), '略過階段須填寫有效原因。')
        require(len(skipped) < len(phase_names), '至少保留一個階段。')
        row.template_snapshot = json.dumps({'id': template.id, 'version': template.version,
                                           'phases': json.loads(template.phases_json), 'skipped_phases': skipped,
                                           'requires_confirmation': template.requires_confirmation}, ensure_ascii=False)
    db.add(row)
    db.flush()
    members = data.get('members', [])
    require(isinstance(members, list) and len(members) <= 100 and all(isinstance(pid, str) for pid in members), '規劃人名單格式不符。')
    for pid in set(members + [actor.id]):
        person(db, actor, pid)
        db.add(ProjectMember(project_id=row.id, person_id=pid))
    db.flush()
    if template:
        from .services import create_task
        for phase in json.loads(template.phases_json):
            if phase not in skipped:
                create_task(db, actor, {'title': phase, 'phase': phase, 'project_id': row.id,
                            'owner_id': owner.id, 'assignment': 'self' if owner.id == actor.id else 'assigned',
                            'commitment': phase + '成果', 'requires_confirmation': template.requires_confirmation})
    audit(db, actor, row, 'created', after={'name': row.name, 'goal': row.goal, 'template': json.loads(row.template_snapshot)})
    db.flush()
    return row


def edit(db, actor, row, data):
    fields(data, {'revision', 'name', 'goal', 'reason', 'next_action', 'wait_for', 'follow_on'})
    expected(row, data)
    require(not row.cleaned, "專案內容已到期清理。", 409)
    require(can_plan_project(db, actor, row), '無權規劃專案。', 403)
    ancestors_open(db, row)
    require(not row.cancelled and row.status not in {'review', 'done'}, '請先恢復或重開專案。', 409)
    reason = text(data, 'reason', required=True)
    before = {}
    for key in {'name', 'goal', 'next_action', 'wait_for', 'follow_on'} & set(data):
        before[key] = getattr(row, key)
        setattr(row, key, day(data[key]) if key == 'follow_on' else text(data, key, 200 if key == 'name' else 8000, key in {'name', 'goal'}))
    if row.status == 'waiting':
        require(row.wait_for and row.next_action and row.follow_on, '等待專案須保留等待原因與追蹤。')
    audit(db, actor, row, 'edited', reason, before, {k: getattr(row, k) for k in before})
    db.flush()
    return row


def action(db, actor, row, data):
    fields(data, {'revision', 'action', 'status', 'reason', 'next_action', 'wait_for', 'follow_on', 'result',
                  'evidence_task_id', 'version_id', 'submission_id', 'decision'})
    expected(row, data)
    require(not row.cleaned, "專案內容已到期清理。", 409)
    ancestors_open(db, row)
    operation = data.get('action')
    reason = text(data, 'reason', required=True)
    before = {'status': row.status, 'cancelled': row.cancelled}
    if operation in {'cancel', 'restore'}:
        require(capability(db, actor, row, 'cancel'), '無權取消或恢復專案。', 403)
        if operation == 'cancel':
            all_closed(db, row)
        row.cancelled = operation == 'cancel'
    else:
        require(not row.cancelled, '請先恢復專案。', 409)
        if operation == 'submit':
            require(actor.id == row.owner_id, '僅專案主責可提交。', 403)
            require(row.status in {'todo', 'doing'}, '請先解除等待或撤回提交。', 409)
            ids = all_closed(db, row)
            task = readable_task(db, actor, data.get('evidence_task_id'))
            require(task.project_id in ids and task.status == 'done' and not task.cancelled, '請選擇本案已完成交付。', 409)
            version = db.get(DocumentVersion, data.get('version_id'))
            require(can_read_version(db, actor, task, version) and task.adopted_version_id == version.id,
                    '請選擇已完成交付的採用版本。', 409)
            read_blob(version)
            submission = ProjectSubmission(project_id=row.id, project_revision=row.revision,
                                            author_id=actor.id, result=text(data, 'result', required=True), goal=row.goal,
                                            evidence_task_id=task.id, evidence_version_id=version.id, evidence_sha256=version.sha256)
            db.add(submission)
            db.flush()
            row.status, row.current_submission_id = 'review', submission.id
        elif operation == 'confirm':
            require(capability(db, actor, row, 'confirm'), '無權確認專案成果。', 403)
            require(row.status == 'review' and row.current_submission_id == data.get('submission_id'), '提交已變更。', 409)
            submission = db.get(ProjectSubmission, row.current_submission_id)
            require(submission is not None and submission.author_id != actor.id, '不可確認自己提交的成果。', 403)
            decision = data.get('decision')
            require(decision in {'approve', 'return'}, '確認決定不符。')
            if decision == 'approve':
                all_closed(db, row)
                task = readable_task(db, actor, submission.evidence_task_id)
                version = db.get(DocumentVersion, submission.evidence_version_id)
                require(can_read_version(db, actor, task, version) and version.sha256 == submission.evidence_sha256,
                        '提交佐證不可核對。', 409)
                read_blob(version)
            db.add(ProjectConfirmation(submission_id=submission.id, actor_id=actor.id, decision=decision, reason=reason))
            row.status = 'done' if decision == 'approve' else 'doing'
            if decision == 'return':
                row.current_submission_id = None
        elif operation in {'reopen', 'withdraw'}:
            require(actor.id == row.owner_id, '僅專案主責可撤回或重開。', 403)
            require(row.status == ('done' if operation == 'reopen' else 'review'), '目前狀態不符。', 409)
            row.status, row.current_submission_id = 'doing', None
        elif operation == 'status':
            require(can_plan_project(db, actor, row), '無權變更專案狀態。', 403)
            target = data.get('status')
            require(row.status in {'todo', 'doing', 'waiting'} and target in {'todo', 'doing', 'waiting'}, '請使用成果提交與確認流程。', 409)
            row.next_action = text(data, 'next_action') or row.next_action
            row.follow_on = day(data.get('follow_on')) or row.follow_on
            if target in {'doing', 'waiting'}:
                require(row.next_action and row.follow_on, '請填寫下一步與追蹤日。')
            if target == 'waiting':
                row.wait_for = text(data, 'wait_for', required=True)
            else:
                row.wait_for = ''
            row.status = target
        else:
            raise DomainError('專案操作不符。')
    audit(db, actor, row, operation, reason, before, {'status': row.status, 'cancelled': row.cancelled})
    db.flush()
    return row
