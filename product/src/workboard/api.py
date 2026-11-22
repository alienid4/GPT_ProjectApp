import hashlib
import json
import io
from datetime import timedelta

from flask import Blueprint, current_app, g, jsonify, request, send_file
from sqlalchemy import delete, select

from .auth import authenticated, person_payload
from .errors import DomainError, require
from .models import (AccessReader, Comment, Confirmation, DocumentVersion, Event,
                     LoginSession, Person, Project, RequestKey, Submission, Task, TaskAccess, VersionReader, utcnow)
from .policy import (ROLE_CAPABILITIES, actor_roles, can_edit, can_manage, can_read,
                     can_read_version, can_read_version_record, global_capability, readable_task, task_read_clause, work_eligible)
from . import services as svc

api = Blueprint("api", __name__, url_prefix="/api")


def body():
    data = request.get_json(silent=True)
    require(isinstance(data, dict), "請提供 JSON 物件。")
    return data


def keyed(data, callback, kind="task"):
    key = request.headers.get("Idempotency-Key", "")
    require(8 <= len(key) <= 64 and key.isascii(), "請提供有效的操作識別。")
    fingerprint = hashlib.sha256((request.method + request.path + json.dumps(data, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
    saved = g.db.get(RequestKey, (g.user.id, key))
    if saved:
        require(saved.request_hash == fingerprint, "操作識別已用於其他內容。", 409, "idempotency_conflict")
        require(saved.expires_at > utcnow(), "操作識別已過期，請重新載入。", 409)
        # Current authorization always wins over the stored response.
        if kind == 'project':
            from .projects import readable, payload
            current = readable(g.db, g.user, saved.resource_id)
            return jsonify({'project': payload(g.db, g.user, current), 'replayed': True})
        current = readable_task(g.db, g.user, saved.resource_id)
        return jsonify({"task": svc.task_payload(g.db, g.user, current), "replayed": True})
    updated = callback()
    g.db.flush()
    g.db.add(RequestKey(actor_id=g.user.id, key=key, request_hash=fingerprint,
                        resource_id=updated.id, operation=request.method + request.path[:40],
                        result_json=json.dumps({"id": updated.id}), expires_at=utcnow() + timedelta(days=7)))
    if kind == 'project':
        from .projects import payload
        return jsonify({'project': payload(g.db, g.user, updated), 'replayed': False})
    if can_read(g.db, g.user, updated):
        return jsonify({"task": svc.task_payload(g.db, g.user, updated), "replayed": False})
    return jsonify({"ok": True, "access_ended": True})


@api.get('/projects')
@authenticated
def list_projects():
    from .projects import payload
    from .policy import can_read_project
    rows = [p for p in g.db.scalars(select(Project).where(Project.department == g.user.department).order_by(Project.code)) if can_read_project(g.db, g.user, p)]
    query = request.args.get('q', '').casefold()[:200]
    if query:
        rows = [p for p in rows if query in p.name.casefold() or query in p.code.casefold()]
    return jsonify({'items': [payload(g.db, g.user, p) for p in rows], 'scope': '目前授權範圍'})


@api.post('/projects')
@authenticated
def create_project():
    from .projects import create
    data = body()
    return keyed(data, lambda: create(g.db, g.user, data), 'project')


@api.get('/projects/<project_id>')
@authenticated
def get_project(project_id):
    from .projects import readable, payload
    from .models import ProjectSubmission, ProjectConfirmation
    row = readable(g.db, g.user, project_id)
    submissions = []
    for s in g.db.scalars(select(ProjectSubmission).where(ProjectSubmission.project_id == row.id).order_by(ProjectSubmission.created_at)):
        task = g.db.get(Task, s.evidence_task_id)
        version = g.db.get(DocumentVersion, s.evidence_version_id)
        if not can_read_version_record(g.db, g.user, task, version):
            continue
        submissions.append({'id': s.id, 'result': s.result, 'goal': s.goal, 'author_id': s.author_id,
                            'evidence_task_id': s.evidence_task_id, 'version_id': s.evidence_version_id,
                            'sha256': s.evidence_sha256,
                            'decisions': [{'decision': c.decision, 'reason': c.reason, 'actor_id': c.actor_id}
                                          for c in g.db.scalars(select(ProjectConfirmation).where(ProjectConfirmation.submission_id == s.id))]})
    return jsonify({'project': payload(g.db, g.user, row), 'submissions': submissions})


@api.patch('/projects/<project_id>')
@authenticated
def edit_project(project_id):
    from .projects import readable, edit
    row, data = readable(g.db, g.user, project_id), body()
    return keyed(data, lambda: edit(g.db, g.user, row, data), 'project')


@api.post('/projects/<project_id>/actions')
@authenticated
def project_action(project_id):
    from .projects import readable, action
    row, data = readable(g.db, g.user, project_id), body()
    return keyed(data, lambda: action(g.db, g.user, row, data), 'project')


@api.get("/people")
@authenticated
def people():
    require(work_eligible(g.user) or global_capability(g.user, "users"), "無權選取人員。", 403)
    rows = g.db.scalars(select(Person).where(Person.department == g.user.department, Person.active.is_(True), Person.directory_active.is_(True)).order_by(Person.name))
    return jsonify({"items": [person_payload(p) for p in rows if work_eligible(p)]})


@api.get("/tasks")
@authenticated
def tasks():
    stmt = select(Task).where(task_read_clause(g.user))
    query = request.args.get("q", "").strip()[:200]
    if query:
        stmt = stmt.where(Task.title.contains(query, autoescape=True) | Task.code.contains(query, autoescape=True))
    for key in ["project_id", "parent_id", "source", "status", "owner_id"]:
        if request.args.get(key):
            stmt = stmt.where(getattr(Task, key) == request.args[key])
    view = request.args.get("view", "active")
    require(view in {"active", "cancelled", "all"}, "工作檢視不符。")
    if view != "all":
        stmt = stmt.where(Task.cancelled.is_(view == "cancelled"))
    sort_key = request.args.get("sort", "created_at")
    require(sort_key in {"code", "title", "priority", "status", "target_on", "created_at"}, "排序欄位不符。")
    column = getattr(Task, sort_key)
    # Missing dates stay last in both sort directions.
    from sqlalchemy import case
    stmt = stmt.order_by(case((column.is_(None), 1), else_=0), column.desc() if request.args.get("direction") == "desc" else column.asc(), Task.id)
    return jsonify({"items": [svc.task_payload(g.db, g.user, row) for row in g.db.scalars(stmt)],
                    "scope": "目前授權範圍", "as_of": utcnow().isoformat() + "Z"})


@api.post("/tasks")
@authenticated
def create_task():
    data = body()
    return keyed(data, lambda: svc.create_task(g.db, g.user, data))


@api.get("/tasks/<task_id>")
@authenticated
def get_task(task_id):
    task = readable_task(g.db, g.user, task_id)
    comments = list(g.db.scalars(select(Comment).where(Comment.task_id == task.id).order_by(Comment.created_at, Comment.id)))
    events = list(g.db.scalars(select(Event).where(Event.resource_type == "task", Event.resource_id == task.id,
                                                  Event.visibility == "read").order_by(Event.sequence)))
    return jsonify({"task": svc.task_payload(g.db, g.user, task),
                    "comments": [{"id": c.id, "body": "留言已撤回" if c.withdrawn else c.body,
                                  "author_id": c.author_id, "author": g.db.get(Person, c.author_id).name,
                                  "parent_id": c.parent_id, "revision": c.revision, "withdrawn": c.withdrawn,
                                  "created_at": c.created_at.isoformat() + "Z"} for c in comments],
                    "events": [{"id": e.id, "action": e.action, "reason": e.reason, "actor_id": e.actor_id,
                                "created_at": e.created_at.isoformat() + "Z", "before": json.loads(e.before_json),
                                "after": json.loads(e.after_json)} for e in events]})


@api.patch("/tasks/<task_id>")
@authenticated
def edit_task(task_id):
    task, data = readable_task(g.db, g.user, task_id), body()
    return keyed(data, lambda: svc.edit_task(g.db, g.user, task, data))


@api.post("/tasks/<task_id>/<operation>")
@authenticated
def task_operation(task_id, operation):
    task, data = readable_task(g.db, g.user, task_id), body()
    operations = {"status": svc.transition, "commitment": svc.change_commitment,
                  "arrangement": svc.arrange, "control": svc.control, "plan": svc.plan_task}
    from .evidence import submit, adopt, confirm
    operations.update(submit=submit, adopt=adopt, confirm=confirm)
    require(operation in operations, "找不到操作。", 404)
    return keyed(data, lambda: operations[operation](g.db, g.user, task, data))


@api.get('/tasks/<task_id>/files')
@authenticated
def files(task_id):
    from .storage import version_payload
    task = readable_task(g.db, g.user, task_id)
    versions = list(g.db.scalars(select(DocumentVersion).where(DocumentVersion.task_id == task.id).order_by(DocumentVersion.created_at, DocumentVersion.number)))
    visible = [v for v in versions if can_read_version_record(g.db, g.user, task, v)]
    ids = {v.id for v in visible}
    submissions = list(g.db.scalars(select(Submission).where(Submission.task_id == task.id).order_by(Submission.created_at)))
    editable = request.args.get('editor') == '1' and can_edit(g.db, g.user, task)
    items = []
    for version in visible:
        item = version_payload(version)
        if editable:
            item['readers'] = list(g.db.scalars(select(VersionReader.person_id).where(
                VersionReader.version_id == version.id))) if version.restricted else None
        items.append(item)
    candidates = []
    if editable:
        candidates = [p.id for p in g.db.scalars(select(Person).where(
            Person.department == g.user.department, Person.active.is_(True), Person.directory_active.is_(True)))
            if can_read(g.db, p, task)]
    return jsonify({'items': items, 'reader_candidates': candidates, 'submissions': [
        {'id': s.id, 'author_id': s.author_id, 'result': s.result, 'commitment': s.commitment,
         'version_id': s.version_id, 'sha256': s.version_sha256, 'created_at': s.created_at.isoformat() + 'Z',
         'decisions': [{'decision': c.decision, 'actor_id': c.actor_id, 'reason': c.reason,
                        'created_at': c.created_at.isoformat() + 'Z'} for c in g.db.scalars(select(Confirmation).where(Confirmation.submission_id == s.id).order_by(Confirmation.created_at))]}
        for s in submissions if s.version_id in ids]})


@api.post('/tasks/<task_id>/files')
@authenticated
def upload_file(task_id):
    from .storage import MAX_FILE_BYTES, upload
    task = readable_task(g.db, g.user, task_id)
    require(set(request.form) <= {'document_id', 'readers'}, '上傳欄位不符。')
    require(set(request.files) == {'file'}, '請選擇一份附件。')
    file = request.files['file']
    raw = file.stream.read(MAX_FILE_BYTES + 1)
    require(len(raw) <= MAX_FILE_BYTES, '附件超過大小限制。', 413)
    file.stream.seek(0)
    readers = None
    if 'readers' in request.form:
        try:
            readers = json.loads(request.form['readers'])
        except ValueError:
            raise DomainError('附件名單格式不符。') from None
    document_id = request.form.get('document_id') or None
    data = {'filename': file.filename, 'sha256': hashlib.sha256(raw).hexdigest(), 'document_id': document_id, 'readers': readers}
    def perform():
        upload(g.db, g.user, task, file, document_id, readers)
        return task
    return keyed(data, perform)


@api.get('/files/<version_id>/download')
@authenticated
def download(version_id):
    from .storage import read_blob
    version = g.db.get(DocumentVersion, version_id)
    require(version is not None, '找不到附件。', 404)
    task = g.db.get(Task, version.task_id)
    require(can_read_version(g.db, g.user, task, version), '找不到附件。', 404)
    contents = read_blob(version)
    # Authorize and verify all bytes before committing or sending the response.
    return send_file(io.BytesIO(contents), mimetype='application/octet-stream', as_attachment=True,
                     download_name=version.filename, conditional=False, etag=False, max_age=0)


@api.post("/tasks/<task_id>/comments")
@authenticated
def add_comment(task_id):
    task, data = readable_task(g.db, g.user, task_id), body()
    def perform():
        svc.comment(g.db, g.user, task, data)
        return task
    return keyed(data, perform)


@api.patch("/tasks/<task_id>/comments/<comment_id>")
@authenticated
def edit_comment(task_id, comment_id):
    task, data = readable_task(g.db, g.user, task_id), body()
    row = g.db.get(Comment, comment_id)
    require(row is not None and row.task_id == task.id, "找不到留言。", 404)
    def perform():
        svc.comment(g.db, g.user, task, data, row)
        return task
    return keyed(data, perform)


@api.get("/tasks/<task_id>/access")
@authenticated
def get_access(task_id):
    task = readable_task(g.db, g.user, task_id)
    require(can_manage(g.db, g.user, task, "share") or can_manage(g.db, g.user, task, "confidential"), "無權管理存取。", 403)
    acl = g.db.get(TaskAccess, task.id)
    readers = list(g.db.scalars(select(AccessReader).where(AccessReader.task_id == task.id)))
    return jsonify({"revision": acl.revision, "assignment_revision": task.revision,
                    "confidential": acl.confidential, "readers": [{"person_id": p.person_id, "kind": p.kind} for p in readers],
                    "currently_readable": [p.id for p in g.db.scalars(select(Person).where(Person.department == task.department)) if can_read(g.db, p, task)]})


@api.put("/tasks/<task_id>/access")
@authenticated
def set_access(task_id):
    task, data = readable_task(g.db, g.user, task_id), body()
    svc.fields(data, {"revision", "assignment_revision", "kind", "readers", "confidential", "reason", "acknowledge_excluded"})
    acl = g.db.get(TaskAccess, task.id)
    kind = data.get("kind")
    require(kind in {"share", "confidential"}, "存取操作不符。")
    require(can_manage(g.db, g.user, task, kind), "無權調整存取。", 403)
    svc.expected(acl, data)
    reason = svc.text(data, "reason", required=True)
    ids = data.get("readers", [])
    require(isinstance(ids, list) and len(ids) <= 100 and all(isinstance(pid, str) for pid in ids), "名單格式不符。")
    candidates = [svc.person(g.db, g.user, pid) for pid in set(ids)]
    before_readers = list(g.db.scalars(select(AccessReader.person_id).where(AccessReader.task_id == task.id, AccessReader.kind == kind)))
    before = {"confidential": acl.confidential, "readers": before_readers}
    if kind == "confidential":
        new_secret = data.get("confidential")
        require(type(new_secret) is bool, "機密分類格式不符。")
        if new_secret:
            require(any(has_capability_for_secret(p, task) for p in candidates), "須保留具有機密管理能力的人員。", 409)
            excluded = {p.id for p in g.db.scalars(select(Person).where(Person.department == task.department)) if can_read(g.db, p, task)} - set(ids)
            if excluded:
                require(data.get("acknowledge_excluded") is True and data.get("assignment_revision") == task.revision,
                        "請核對最新安排並確認撤權影響。", 409, "exclusion_acknowledgement_required")
        acl.confidential = new_secret
    g.db.execute(delete(AccessReader).where(AccessReader.task_id == task.id, AccessReader.kind == kind))
    g.db.add_all([AccessReader(task_id=task.id, person_id=p.id, kind=kind) for p in candidates])
    acl.revision += 1
    svc.audit(g.db, g.user, task, "access", reason, before, {"confidential": acl.confidential, "readers": ids}, visibility="access")
    g.db.flush()
    return jsonify({"ok": True, "access_revision": acl.revision, "access_ended": not can_read(g.db, g.user, task)})


def has_capability_for_secret(person, task):
    from .policy import has_capability
    return has_capability(g.db, person, task, "confidential")


@api.get("/admin/people")
@authenticated
def admin_people():
    from .policy import configured_roles
    require(global_capability(g.user, "users"), "無權管理帳號。", 403)
    return jsonify({"mode": current_app.config["APP_MODE"], "items": [
        {**person_payload(p), "roles": sorted(configured_roles(p)), "active": p.active,
         "directory_active": p.directory_active, "revision": p.revision}
        for p in g.db.scalars(select(Person).where(Person.department == g.user.department).order_by(Person.name))]})


@api.patch("/admin/people/<person_id>")
@authenticated
def admin_person(person_id):
    require(global_capability(g.user, "users"), "無權管理帳號。", 403)
    row, data = g.db.get(Person, person_id), body()
    require(row is not None and row.department == g.user.department, "找不到帳號。", 404)
    svc.fields(data, {"revision", "active", "roles", "reason", "revoke_sessions"})
    svc.expected(row, data)
    reason = svc.text(data, "reason", required=True)
    before = {"active": row.active, "roles": json.loads(row.roles_json), "role": row.role}
    if "roles" in data:
        require(current_app.config["APP_MODE"] == "test", "正式角色由 AD 群組管理。", 409)
        require(row.id != g.user.id, '不可變更本人的角色。', 409)
        roles = data["roles"]
        require(isinstance(roles, list) and roles and all(isinstance(r, str) and r in ROLE_CAPABILITIES for r in roles), "角色格式不符。")
        row.role, row.roles_json = roles[0], json.dumps(sorted(set(roles)))
    if "active" in data:
        require(type(data["active"]) is bool, "啟用狀態格式不符。")
        require(row.id != g.user.id or data["active"], "不可停用本人帳號。", 409)
        row.active = data["active"]
    row.auth_epoch += 1
    g.db.execute(delete(LoginSession).where(LoginSession.person_id == row.id))
    svc.audit(g.db, g.user, row, "eligibility", reason, before, {"active": row.active, "roles": json.loads(row.roles_json)}, visibility="admin")
    g.db.flush()
    return jsonify({"ok": True, "revision": row.revision})
