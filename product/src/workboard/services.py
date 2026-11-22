"""Application operations. HTTP and drag/drop share these authorization checks."""
import hashlib
import json
from datetime import date, timedelta

from sqlalchemy import delete, exists, select, update

from .errors import DomainError, require
from .models import (AccessReader, Comment, Confirmation, DocumentVersion, Event,
                     Person, Project, ProjectMember, RequestKey, Sequence, Submission,
                     Task, TaskAccess, TaskRelation, Template, utcnow)
from .policy import (can_cancel, can_comment, can_edit, can_finalize, can_manage,
                     can_pause, can_plan_project, can_read, can_read_project,
                     can_read_version, enabled, global_capability, has_capability,
                     readable_task, task_permissions, task_read_clause, work_eligible)


STATUSES = {"todo", "doing", "waiting", "review", "done"}


def text(data, key, maximum=8000, required=False):
    value = data.get(key, "")
    require(isinstance(value, str) and len(value) <= maximum, f"{key} 格式或長度不符。")
    value = value.strip()
    require(not required or bool(value), "請填寫必要內容。")
    return value


def day(value):
    if value is None or value == "":
        return None
    require(isinstance(value, str), "日期格式不符。")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DomainError("日期格式應為 YYYY-MM-DD。") from None


def fields(data, allowed):
    require(isinstance(data, dict), "資料格式不符。")
    require(not (set(data) - set(allowed)), "包含不允許變更的欄位。")


def expected(record, data, key="revision"):
    require(type(data.get(key)) is int and data[key] == record.revision,
            "資料已變更，請重新載入並核對。", 409, "revision_conflict")


def person(db, actor, person_id):
    candidate = db.get(Person, person_id) if isinstance(person_id, str) else None
    require(work_eligible(candidate) and candidate.department == actor.department,
            "人員目前不可選用。", 400, "ineligible_person")
    return candidate


def next_number(db, key):
    # Production migrations initialize the event counter. Per-year sequences
    # are created under the application transaction lock; uniqueness is also
    # enforced by the DB, so concurrent creation can fail but never duplicate.
    row = db.get(Sequence, key)
    if row is None:
        row = Sequence(key=key, value=0)
        db.add(row)
        db.flush()
    changed = db.execute(update(Sequence).where(Sequence.key == key)
                         .values(value=Sequence.value + 1)).rowcount
    require(changed == 1, "編號暫時無法取得。", 503)
    db.expire(row)
    # UPDATE holds the row write lock until commit; this SELECT therefore
    # reads our own increment without relying on SQLite 3.35 RETURNING.
    return db.scalar(select(Sequence.value).where(Sequence.key == key))


def fixed_code(db, prefix):
    year = utcnow().year
    return f"{prefix}{year}{next_number(db, prefix + str(year)):03d}"


def audit(db, actor, resource, action, reason="", before=None, after=None, visibility="read"):
    kind = "task" if isinstance(resource, Task) else "project" if isinstance(resource, Project) else "person"
    db.add(Event(sequence=next_number(db, "events"), resource_type=kind,
                 resource_id=resource.id, actor_id=actor.id, action=action, reason=reason,
                 before_json=json.dumps(before or {}, ensure_ascii=False, default=str),
                 after_json=json.dumps(after or {}, ensure_ascii=False, default=str), visibility=visibility))


def task_payload(db, actor, task):
    acl = db.get(TaskAccess, task.id)
    result = {key: getattr(task, key) for key in [
        "id", "code", "title", "description", "project_id", "parent_id", "phase", "source", "kind",
        "creator_id", "owner_id", "proposed_owner_id", "assignment", "acknowledged", "status",
        "priority", "requires_confirmation", "result", "next_action", "wait_for", "commitment",
        "paused", "pause_reason", "cancelled", "cleaned", "adopted_version_id", "current_submission_id", "revision"]}
    for key in ["follow_on", "target_on", "planned_start", "planned_end", "pause_review_on", "created_at", "expires_at"]:
        value = getattr(task, key)
        result[key] = value.isoformat() if value else None
    from .planning import visible_leaf_dates
    start, end = visible_leaf_dates(db, actor, [task])
    result['planned_start'] = start.isoformat() if start else None
    result['planned_end'] = end.isoformat() if end else None
    result.update(confidential=acl.confidential, access_revision=acl.revision,
                  permissions=task_permissions(db, actor, task))
    owner = db.get(Person, task.owner_id) if task.owner_id else None
    result["owner_name"] = owner.name if owner else "待安排"
    # Do not leak a project or parent title just because one task was shared.
    project = db.get(Project, task.project_id) if task.project_id else None
    if project and not can_read_project(db, actor, project):
        result["project_id"] = None
    parent = db.get(Task, task.parent_id) if task.parent_id else None
    if parent and not can_read(db, actor, parent):
        result["parent_id"] = None
    result["collaborators"] = [p.id for p in db.scalars(select(Person).join(TaskRelation, TaskRelation.person_id == Person.id)
        .where(TaskRelation.task_id == task.id, TaskRelation.kind == "collaborator"))]
    return result


def open_execution(task, db=None):
    require(not task.cleaned, '工作內容已到期清理。', 409)
    require(not task.cancelled and not task.paused, "工作已取消或暫停。", 409, "work_suspended")
    require(task.assignment not in {"proposed", "unassigned"}, "請先完成工作安排。", 409)
    if db is not None:
        if task.project_id:
            from .projects import ancestors_open
            project = db.get(Project, task.project_id)
            require(project is not None and not project.cleaned and not project.cancelled and project.status not in {'review', 'done'}, '請先恢復或重開專案。', 409)
            ancestors_open(db, project)
        seen, parent_id = {task.id}, task.parent_id
        while parent_id:
            require(parent_id not in seen and len(seen) <= 32, '工作階層異常。', 409)
            seen.add(parent_id)
            parent = db.get(Task, parent_id)
            require(parent is not None and parent.project_id == task.project_id, '工作階層異常。', 409)
            require(not parent.cleaned and not parent.cancelled and not parent.paused and parent.status not in {'review', 'done'}, '請先恢復或重開上層工作。', 409)
            parent_id = parent.parent_id


def editable_execution(db, actor, task, data):
    require(can_edit(db, actor, task), "無權編輯工作。", 403)
    expected(task, data)
    open_execution(task, db)
    require(task.status not in {"review", "done"}, "請先撤回提交或重新開啟工作。", 409)


def children_closed(db, task):
    # Checks all descendants by walking direct children; no hidden counts leak.
    pending = [task.id]
    visited = set()
    while pending:
        current = pending.pop()
        require(current not in visited, "工作階層異常，請洽維護人員。", 409)
        visited.add(current)
        children = list(db.scalars(select(Task).where(Task.parent_id == current, Task.deleted.is_(False))))
        require(all(child.status == "done" or child.cancelled for child in children), "仍有未結事項，請核對下層工作。", 409)
        pending.extend(child.id for child in children)


def create_task(db, actor, data):
    fields(data, {"title", "description", "source", "kind", "project_id", "parent_id", "phase", "owner_id",
                  "assignment", "requires_confirmation", "collaborators", "commitment", "follow_on", "target_on",
                  "template_id"})
    require(work_eligible(actor), "無權建立工作。", 403)
    source = data.get("source", "self")
    require(source in {"self", "adhoc", "assigned", "project"}, "工作來源不符。")
    project_id, parent_id = data.get("project_id"), data.get("parent_id")
    project = db.get(Project, project_id) if isinstance(project_id, str) else None
    if project_id:
        require(can_plan_project(db, actor, project), "找不到可規劃的專案。", 404)
        from .projects import ancestors_open
        require(not project.cleaned and not project.cancelled and project.status not in {'review', 'done'}, '請先恢復或重開專案。', 409)
        ancestors_open(db, project)
        source = "project"
    require(source != "project" or project is not None, "請選擇專案。")
    parent = readable_task(db, actor, parent_id) if parent_id else None
    if parent:
        require(can_edit(db, actor, parent) or can_manage(db, actor, parent, "dispatch"), "無權分解工作。", 403)
        require(parent.project_id == project_id, "子工作必須屬於相同專案。")
        require(parent.status not in {"review", "done"} and not parent.cleaned and not parent.cancelled and not parent.paused,
                "請先恢復上層工作。", 409)
    target = person(db, actor, data.get("owner_id") or actor.id)
    assignment = data.get("assignment", "self" if target.id == actor.id else "proposed")
    require(assignment in {"self", "proposed", "assigned"}, "安排方式不符。")
    require(assignment != "self" or target.id == actor.id, "不能以自主工作替他人承諾。", 403)
    require(assignment != "proposed" or target.id != actor.id, "本人工作請使用自主安排。")
    task = Task(code=fixed_code(db, "work"), title=text(data, "title", 200, True),
                description=text(data, "description"), department=actor.department,
                source=source, kind=text(data, "kind", 80) or "一般工作", project_id=project_id,
                parent_id=parent_id, phase=text(data, "phase", 80), creator_id=actor.id,
                owner_id=target.id if assignment != "proposed" else None,
                proposed_owner_id=target.id if assignment == "proposed" else None,
                assignment=assignment, acknowledged=assignment == "self",
                commitment=text(data, "commitment"), follow_on=day(data.get("follow_on")),
                target_on=day(data.get("target_on")))
    if assignment == "assigned":
        require(has_capability(db, actor, task, "dispatch"), "無權正式派工。", 403)
    confirmation = data.get("requires_confirmation", False)
    require(type(confirmation) is bool, "確認條件格式不符。")
    task.requires_confirmation = confirmation or bool(parent and parent.requires_confirmation)
    if project:
        task.requires_confirmation |= bool(json.loads(project.template_snapshot or '{}').get('requires_confirmation'))
    if data.get("template_id"):
        template = db.get(Template, data["template_id"])
        require(template is not None and template.status == "active", "範本不可使用。")
        task.requires_confirmation |= template.requires_confirmation
    if assignment in {"proposed", "assigned"} and not task.follow_on:
        task.follow_on = date.today() + timedelta(days=1)
    db.add(task)
    db.flush()
    # A confidential child inherits the current named list; no group bypass.
    parent_acl = db.get(TaskAccess, parent.id) if parent else None
    db.add(TaskAccess(task_id=task.id, confidential=bool(parent_acl and parent_acl.confidential)))
    if parent_acl and parent_acl.confidential:
        readers = list(db.scalars(select(AccessReader.person_id).where(AccessReader.task_id == parent.id,
                                                                     AccessReader.kind == "confidential")))
        require(target.id in readers, "安排對象尚無機密存取資格。", 403)
        db.add_all([AccessReader(task_id=task.id, person_id=pid, kind="confidential") for pid in readers])
    collaborators = data.get("collaborators", [])
    require(isinstance(collaborators, list) and len(collaborators) <= 100 and all(isinstance(p, str) for p in collaborators), "協同人格式不符。")
    for pid in set(collaborators):
        collaborator = person(db, actor, pid)
        if parent_acl and parent_acl.confidential:
            require(collaborator.id in readers, "協同人尚無機密存取資格。", 403)
        db.add(TaskRelation(task_id=task.id, person_id=pid, kind="collaborator"))
    audit(db, actor, task, "created", after={"title": task.title, "assignment": assignment, "owner_id": task.owner_id})
    db.flush()
    return task


def edit_task(db, actor, task, data):
    fields(data, {"revision", "title", "description", "result", "next_action", "wait_for", "follow_on", "reason"})
    editable_execution(db, actor, task, data)
    before = {}
    for key in set(data) & {"title", "description", "result", "next_action", "wait_for", "follow_on"}:
        before[key] = getattr(task, key)
        setattr(task, key, day(data[key]) if key == "follow_on" else text(data, key, 200 if key == "title" else 8000, key == "title"))
    if task.status == "doing":
        require(task.next_action and task.follow_on, "進行中工作須有下一步與追蹤日。")
    if task.status == "waiting":
        require(task.wait_for and task.next_action and task.follow_on, "請填寫等待對象、原因、下一步與追蹤日。")
    audit(db, actor, task, "edited", text(data, "reason"), before, {k: getattr(task, k) for k in before})
    db.flush()
    return task


def transition(db, actor, task, data):
    fields(data, {"revision", "status", "reason", "result", "next_action", "wait_for", "follow_on"})
    expected(task, data)
    open_execution(task, db)
    target = data.get("status")
    require(target in STATUSES and target != task.status, "目標狀態不符。")
    reason = text(data, "reason")
    before = {"status": task.status, "result": task.result, "wait_for": task.wait_for,
              "next_action": task.next_action, "follow_on": task.follow_on}
    if task.status in {"review", "done"}:
        require(can_finalize(db, actor, task), "無權撤回或重開工作。", 403)
        require(target == "doing" and reason, "請使用重開或撤回，並填寫原因。", 409)
        task.current_submission_id = None
    else:
        require(can_edit(db, actor, task), "無權變更狀態。", 403)
    if target == "review":
        raise DomainError("請指定成果版本並提交確認。", 409, "submission_required")
    if target == "done":
        require(can_finalize(db, actor, task), "僅主責或代理可完成工作。", 403)
        require(not task.requires_confirmation, "此工作須提交成果確認。", 409, "submission_required")
        require(task.status in {"todo", "doing"}, "請先解除等待。", 409)
        children_closed(db, task)
        task.result = text(data, "result") or task.result
        require(bool(task.result), "請填寫實際成果。")
    if task.status == "waiting":
        require(target == "doing" and reason, "請填寫解除等待原因。", 409)
        task.wait_for = ""
    if target in {"doing", "waiting"}:
        task.next_action = text(data, "next_action") or task.next_action
        task.follow_on = day(data.get("follow_on")) or task.follow_on
        require(task.next_action and task.follow_on and task.commitment, "請填寫承諾成果、下一步與追蹤日。")
    if target == "waiting":
        require(task.status == "doing", "進行中工作才能轉為等待。", 409)
        task.wait_for = text(data, "wait_for", required=True)
    task.status = target
    audit(db, actor, task, "status", reason, before, {k: getattr(task, k) for k in before})
    db.flush()
    return task


def change_commitment(db, actor, task, data):
    fields(data, {"revision", "priority", "commitment", "target_on", "reason"})
    expected(task, data)
    open_execution(task, db)
    require(task.status not in {"review", "done"}, "請先撤回或重開工作。", 409)
    reason = text(data, "reason", required=True)
    before = {}
    for key in {"priority", "commitment", "target_on"} & set(data):
        require(can_manage(db, actor, task, "priority" if key == "priority" else "commit"), "無權變更此承諾。", 403)
        before[key] = getattr(task, key)
        if key == "priority":
            require(type(data[key]) is int and 1 <= data[key] <= 4, "優先順序不符。")
            task.priority = data[key]
        elif key == "target_on":
            task.target_on = day(data[key])
        else:
            task.commitment = text(data, key, required=True)
    require(before, "沒有可變更的項目。")
    audit(db, actor, task, "commitment", reason, before, {k: getattr(task, k) for k in before})
    db.flush()
    return task


def arrange(db, actor, task, data):
    fields(data, {"revision", "action", "person_id", "reason", "follow_on"})
    expected(task, data)
    require(not task.cleaned, "工作內容已到期清理。", 409)
    action = data.get("action")
    reason = text(data, "reason")
    require(not task.cancelled and not task.paused and task.status not in {"review", "done"}, "目前不可變更安排。", 409)
    before = {k: getattr(task, k) for k in ["owner_id", "proposed_owner_id", "assignment", "acknowledged"]}
    if action in {"accept", "decline"}:
        require(task.assignment == "proposed" and task.proposed_owner_id == actor.id, "建議已失效或無權處理。", 409)
        if action == "accept":
            task.owner_id, task.assignment, task.acknowledged = actor.id, "accepted", True
        else:
            require(reason, "請填寫婉拒原因。")
            task.assignment, task.owner_id = "unassigned", None
        task.proposed_owner_id = None
    elif action == "acknowledge":
        require(task.assignment == "assigned" and task.owner_id == actor.id, "無權確認收到此安排。", 403)
        task.acknowledged = True
    elif action in {"assign", "propose"}:
        target = person(db, actor, data.get("person_id"))
        if db.get(TaskAccess, task.id).confidential:
            require(can_read(db, target, task), "對象尚無機密存取資格。", 403)
        if action == "assign":
            require(can_manage(db, actor, task, "dispatch"), "無權派工。", 403)
            task.owner_id, task.proposed_owner_id, task.assignment = target.id, None, "assigned"
        else:
            require(task.creator_id == actor.id and task.assignment in {"unassigned", "proposed"}, "無權再次提議。", 403)
            task.owner_id, task.proposed_owner_id, task.assignment = None, target.id, "proposed"
        require(reason, "請填寫安排原因。")
        task.acknowledged = False
        task.follow_on = day(data.get("follow_on")) or date.today() + timedelta(days=1)
    elif action in {"add_collaborator", "remove_collaborator", "add_delegate", "remove_delegate"}:
        require(can_manage(db, actor, task, "dispatch"), "無權調整協作安排。", 403)
        target = person(db, actor, data.get("person_id"))
        kind = "delegate" if action.endswith("delegate") else "collaborator"
        if action.startswith("add"):
            if db.get(TaskAccess, task.id).confidential:
                require(can_read(db, target, task), "對象尚無機密存取資格。", 403)
            if db.get(TaskRelation, (task.id, target.id, kind)) is None:
                db.add(TaskRelation(task_id=task.id, person_id=target.id, kind=kind))
        else:
            db.execute(delete(TaskRelation).where(TaskRelation.task_id == task.id, TaskRelation.person_id == target.id, TaskRelation.kind == kind))
        task.revision += 1
        require(reason, "請填寫協作變更原因。")
    else:
        raise DomainError("安排操作不符。")
    audit(db, actor, task, "arrangement", reason, before, {**{k: getattr(task, k) for k in before}, "action": action, "person_id": data.get("person_id")})
    db.flush()
    return task


def control(db, actor, task, data):
    fields(data, {"revision", "action", "reason", "review_on"})
    expected(task, data)
    reason = text(data, "reason", required=True)
    require(not task.cleaned, "工作內容已到期清理。", 409)
    action = data.get("action")
    before = {"cancelled": task.cancelled, "paused": task.paused}
    if action in {"cancel", "restore"}:
        require(can_cancel(db, actor, task), "無權取消或恢復工作。", 403)
        if action == "cancel":
            children_closed(db, task)
        task.cancelled = action == "cancel"
    elif action in {"pause", "resume"}:
        require(can_pause(db, actor, task) and not task.cancelled, "無權暫停或恢復執行。", 403)
        if action == "pause":
            require(task.status != "done", "已完成工作不需暫停。", 409)
            task.pause_review_on = day(data.get("review_on"))
            require(task.pause_review_on, "請填寫下次檢視日。")
        task.paused, task.pause_reason = action == "pause", reason
    else:
        raise DomainError("控制操作不符。")
    audit(db, actor, task, action, reason, before, {"cancelled": task.cancelled, "paused": task.paused})
    db.flush()
    return task


def comment(db, actor, task, data, existing=None):
    fields(data, {"body", "parent_id", "revision", "withdraw"})
    require(not task.cleaned, "工作內容已到期清理。", 409)
    require(can_comment(db, actor, task), "無權留言。", 403)
    if existing:
        require(existing.task_id == task.id and existing.author_id == actor.id, "無權修改他人留言。", 403)
        expected(existing, data)
        require("parent_id" not in data, "不能變更討論關係。")
        before = {"body": existing.body, "withdrawn": existing.withdrawn}
        if data.get("withdraw") is True:
            existing.withdrawn = True
        else:
            require(not existing.withdrawn, "已撤回留言不可修改。", 409)
            existing.body = text(data, "body", required=True)
        row = existing
    else:
        parent_id = data.get("parent_id")
        if parent_id:
            parent = db.get(Comment, parent_id)
            require(parent is not None and parent.task_id == task.id, "討論關係不符。")
        row = Comment(task_id=task.id, author_id=actor.id, body=text(data, "body", required=True), parent_id=parent_id)
        db.add(row)
        before = {}
    db.flush()
    audit(db, actor, task, "comment", before=before, after={"comment_id": row.id, "withdrawn": row.withdrawn}, visibility="comment_audit")
    return row


def plan_task(db, actor, task, data):
    fields(data, {'revision', 'planned_start', 'planned_end', 'reason'})
    editable_execution(db, actor, task, data)
    require(not db.scalar(select(exists().where(Task.parent_id == task.id, Task.deleted.is_(False)))),
            '上層日期由子項彙總，請調整末層工作。', 409)
    start, end = day(data.get('planned_start')), day(data.get('planned_end'))
    require((start is None and end is None) or (start and end and start <= end), '計畫起訖日期不符。')
    reason = text(data, 'reason', required=True)
    before = {'planned_start': task.planned_start, 'planned_end': task.planned_end}
    task.planned_start, task.planned_end = start, end
    audit(db, actor, task, 'plan', reason, before, {'planned_start': start, 'planned_end': end})
    db.flush()
    return task
