"""Immutable submissions bind exact file bytes and never inherit confirmation."""
from sqlalchemy import select

from .errors import require
from .models import Confirmation, DocumentVersion, Submission
from .policy import can_finalize, can_manage, can_read_version
from .services import audit, children_closed, expected, fields, open_execution, text
from .storage import read_blob


def evidence_version(db, actor, task, version_id):
    version = db.get(DocumentVersion, version_id) if isinstance(version_id, str) else None
    require(can_read_version(db, actor, task, version), "找不到可用的成果版本。", 404)
    read_blob(version)
    return version


def submit(db, actor, task, data):
    fields(data, {'revision', 'version_id', 'result', 'reason'})
    expected(task, data)
    open_execution(task, db)
    require(can_finalize(db, actor, task), "僅主責或明確代理可提交。", 403)
    require(task.status in {'todo', 'doing'}, "請先解除等待或撤回既有提交。", 409)
    require(task.requires_confirmation, "例行工作請直接完成。", 409)
    children_closed(db, task)
    version = evidence_version(db, actor, task, data.get('version_id') or task.adopted_version_id)
    result = text(data, 'result') or task.result
    require(result and task.commitment, "請填寫成果及承諾內容。")
    submission = Submission(task_id=task.id, author_id=actor.id, result=result, commitment=task.commitment,
                            version_id=version.id, version_sha256=version.sha256)
    db.add(submission)
    db.flush()
    before = {'status': task.status, 'submission_id': task.current_submission_id}
    task.adopted_version_id, task.current_submission_id = version.id, submission.id
    task.result, task.status = result, 'review'
    audit(db, actor, task, 'submitted', text(data, 'reason'), before, {'status': task.status, 'submission_id': submission.id})
    db.flush()
    return task


def adopt(db, actor, task, data):
    fields(data, {'revision', 'version_id', 'reason'})
    expected(task, data)
    open_execution(task, db)
    require(can_finalize(db, actor, task), "僅主責或明確代理可改採版本。", 403)
    version = evidence_version(db, actor, task, data.get('version_id'))
    require(version.id != task.adopted_version_id, "此版本已採用。", 409)
    reason = text(data, 'reason', required=True)
    before = {'adopted_version_id': task.adopted_version_id, 'status': task.status}
    if task.status in {'review', 'done'}:
        require(task.requires_confirmation, "請先重開已完成的例行工作。", 409)
        children_closed(db, task)
        require(task.result and task.commitment, "缺少成果摘要。", 409)
        submission = Submission(task_id=task.id, author_id=actor.id, result=task.result,
                                commitment=task.commitment, version_id=version.id, version_sha256=version.sha256)
        db.add(submission)
        db.flush()
        task.current_submission_id, task.status = submission.id, 'review'
    task.adopted_version_id = version.id
    audit(db, actor, task, 'adopted', reason, before, {'adopted_version_id': version.id, 'status': task.status})
    db.flush()
    return task


def confirm(db, actor, task, data):
    fields(data, {'revision', 'submission_id', 'decision', 'reason'})
    expected(task, data)
    open_execution(task, db)
    require(can_manage(db, actor, task, 'confirm'), "無權確認成果。", 403)
    require(task.status == 'review' and task.current_submission_id == data.get('submission_id'), "提交已變更，請重新核對。", 409)
    submission = db.get(Submission, task.current_submission_id)
    require(submission is not None and submission.task_id == task.id and actor.id != submission.author_id,
            "確認者不可為本次實際提交者。", 403)
    decision = data.get('decision')
    require(decision in {'approve', 'return'}, "確認決定不符。")
    reason = text(data, 'reason', required=True)
    if decision == 'approve':
        children_closed(db, task)
        version = evidence_version(db, actor, task, submission.version_id)
        require(version.sha256 == submission.version_sha256, "提交依據已不一致。", 409)
        require(submission.version_id == task.adopted_version_id, "採用版本已變更。", 409)
    # Returning a broken submission remains possible; approval requires bytes.
    db.add(Confirmation(submission_id=submission.id, actor_id=actor.id, decision=decision, reason=reason))
    task.status = 'done' if decision == 'approve' else 'doing'
    if decision == 'return':
        task.current_submission_id = None
    audit(db, actor, task, 'confirmed' if decision == 'approve' else 'returned', reason,
          {'status': 'review'}, {'status': task.status, 'submission_id': submission.id})
    db.flush()
    return task
