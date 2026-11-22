import io
import json
import pytest
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select
from conftest import make_work
from workboard.models import DocumentVersion


@pytest.mark.parametrize('failure', ['scan', 'commit'])
def test_failed_upload_removes_only_its_uncommitted_blob(board, monkeypatch, failure):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError
    from workboard.errors import DomainError
    from workboard import storage
    app, _, clients, call = board
    task = make_work(call)
    assert upload(clients, task, contents=b'keep committed bytes').status_code == 200
    original = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][0]
    root = Path(app.config['STORAGE_PATH'])
    before = {p for p in root.rglob('*') if p.is_file()}
    session_class = app.extensions['sessions'].class_
    def fail_commit(session):
        if session.info.get('upload_under_test'):
            raise SQLAlchemyError('injected commit failure')
    if failure == 'scan':
        def failed_scan(_):
            raise DomainError('掃描服務暫時不可用。', 503)
        monkeypatch.setattr(storage, 'scan', failed_scan)
    else:
        original_audit = storage.audit
        def mark_upload(db, *args, **kwargs):
            original_audit(db, *args, **kwargs)
            db.info['upload_under_test'] = True
        monkeypatch.setattr(storage, 'audit', mark_upload)
        event.listen(session_class, 'before_commit', fail_commit)
    try:
        response = upload(clients, task, contents=b'must not remain without a database record')
        assert response.status_code == 503
    finally:
        if failure == 'commit':
            event.remove(session_class, 'before_commit', fail_commit)
    assert {p for p in root.rglob('*') if p.is_file()} == before
    assert call('alice', 'GET', '/api/files/' + original['id'] + '/download').data == b'keep committed bytes'
    assert len(call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items']) == 1


def test_upload_compensation_keeps_committed_or_unverifiable_bytes(board):
    from sqlalchemy.exc import SQLAlchemyError
    from workboard.storage import blob_path, discard_uncommitted_uploads
    app, _, clients, call = board
    task = make_work(call)
    assert upload(clients, task, contents=b'committed bytes').status_code == 200
    version = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][0]
    with app.extensions['sessions']() as db:
        key = db.get(DocumentVersion, version['id']).blob_key
    def unavailable():
        raise SQLAlchemyError('injected verification outage')
    with app.app_context():
        discard_uncommitted_uploads({key}, app.extensions['sessions'])
        assert blob_path(key).read_bytes() == b'committed bytes'
        discard_uncommitted_uploads({key}, unavailable)
        assert blob_path(key).read_bytes() == b'committed bytes'


def test_failed_compensation_is_durable_and_cli_retry_preserves_committed_files(board, monkeypatch):
    from workboard import storage
    from workboard.errors import DomainError
    app, _, clients, call = board
    task = make_work(call)
    assert upload(clients, task, contents=b'preserve me').status_code == 200
    root = Path(app.config['STORAGE_PATH'])
    original = {p for p in root.rglob('*') if p.is_file()}
    unlink = Path.unlink
    def deny_blob_unlink(path, *args, **kwargs):
        if path.parent.name != '.pending-uploads' and len(path.name) == 64:
            raise PermissionError('injected storage outage')
        return unlink(path, *args, **kwargs)
    def reject_scan(_):
        raise DomainError('掃描設定無效。', 503)
    with monkeypatch.context() as fault:
        fault.setattr(storage, 'scan', reject_scan)
        fault.setattr(Path, 'unlink', deny_blob_unlink)
        assert upload(clients, task, contents=b'orphan pending retry').status_code == 503
    assert len(list((root / '.pending-uploads').iterdir())) == 1
    result = app.test_cli_runner().invoke(args=['reconcile-uploads'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {'checked': 1, 'pending': 0}
    assert {p for p in root.rglob('*') if p.is_file()} == original


def test_abrupt_upload_process_exit_is_reconciled_after_restart(board):
    import os
    import subprocess
    import sys
    app, ids, clients, call = board
    task = make_work(call)
    assert upload(clients, task, contents=b'committed before process exit').status_code == 200
    root = Path(app.config['STORAGE_PATH'])
    original = {p for p in root.rglob('*') if p.is_file()}
    script = '''
import io,json,os,sys
from sqlalchemy import update
from werkzeug.datastructures import FileStorage
from workboard.app import create_app
from workboard.models import Person,Task,Sequence
from workboard.storage import upload
app=create_app(json.loads(sys.argv[1]))
with app.app_context(), app.extensions['sessions'].begin() as db:
 db.execute(update(Sequence).where(Sequence.key=='application_transaction').values(value=Sequence.value))
 upload(db,db.get(Person,sys.argv[2]),db.get(Task,sys.argv[3]),FileStorage(stream=io.BytesIO(b'abrupt exit bytes'),filename='abrupt.txt'))
 os._exit(73)
'''
    config = {k: app.config[k] for k in ['TESTING', 'APP_MODE', 'DATABASE_URL', 'STORAGE_PATH']}
    result = subprocess.run([sys.executable, '-c', script, json.dumps(config), ids['alice'], task['id']],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
        capture_output=True, timeout=20)
    assert result.returncode == 73, result.stderr.decode(errors='replace')
    assert len(list((root / '.pending-uploads').iterdir())) == 1
    assert len(call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items']) == 1
    result = app.test_cli_runner().invoke(args=['reconcile-uploads'])
    assert result.exit_code == 0, result.output
    assert {p for p in root.rglob('*') if p.is_file()} == original


@pytest.mark.parametrize('reject', [False, True])
def test_journal_reconciliation_waits_for_inflight_upload(board, monkeypatch, reject):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from workboard import storage
    from workboard.errors import DomainError
    app, _, clients, call = board
    task = make_work(call)
    entered, release, retry_started = Event(), Event(), Event()
    def blocked_scan(_):
        entered.set()
        assert release.wait(timeout=5)
        if reject:
            raise DomainError('注入掃描故障。', 503)
        return 'test_only'
    monkeypatch.setattr(storage, 'scan', blocked_scan)
    def retry():
        with app.app_context():
            retry_started.set()
            return storage.reconcile_upload_journal(app.extensions['sessions'], 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        uploading = pool.submit(upload, clients, task, contents=b'concurrent upload')
        assert entered.wait(timeout=2)
        retrying = pool.submit(retry)
        assert retry_started.wait(timeout=2)
        try:
            with pytest.raises(TimeoutError):
                retrying.result(timeout=0.2)
        finally:
            release.set()
        assert uploading.result(timeout=5).status_code == (503 if reject else 200)
        retrying.result(timeout=5)
    versions = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items']
    assert len(versions) == (0 if reject else 1)
    if versions:
        assert call('alice', 'GET', '/api/files/' + versions[0]['id'] + '/download').data == b'concurrent upload'
    assert list((Path(app.config['STORAGE_PATH']) / '.pending-uploads').iterdir()) == []


def upload(clients, task, who='alice', contents=b'first evidence', filename='report.txt', document_id=None, readers=None, key=None):
    client, csrf = clients[who]
    data = {'file': (io.BytesIO(contents), filename)}
    if document_id:
        data['document_id'] = document_id
    if readers is not None:
        data['readers'] = json.dumps(readers)
    return client.post('/api/tasks/' + task['id'] + '/files', data=data,
                       headers={'X-CSRF-Token': csrf, 'Idempotency-Key': key or str(uuid4())})


def test_real_bytes_download_and_current_authorization(board):
    _, ids, clients, call = board
    task = make_work(call, collaborators=[ids['bob']])
    assert upload(clients, task, contents='實際成果內容'.encode()).status_code == 200
    files = call('alice', 'GET', f"/api/tasks/{task['id']}/files").json['items']
    assert len(files) == 1 and files[0]['scan_status'] == 'test_only'
    path = '/api/files/' + files[0]['id'] + '/download'
    assert call('bob', 'GET', path).data == '實際成果內容'.encode()
    assert call('bob', 'GET', path).headers['Content-Disposition'].startswith('attachment;')
    assert call('outsider', 'GET', path).status_code == 404
    call('chief', 'POST', f"/api/tasks/{task['id']}/arrangement", {'revision': task['revision'], 'action': 'remove_collaborator', 'person_id': ids['bob'], 'reason': '離開本工作'})
    assert call('bob', 'GET', path).status_code == 404


def test_new_upload_does_not_inherit_confirmation_and_readoption_resubmits(board):
    _, _, clients, call = board
    task = make_work(call, requires_confirmation=True)
    base = '/api/tasks/' + task['id']
    assert upload(clients, task).status_code == 200
    first = call('alice', 'GET', base + '/files').json['items'][0]
    submitted = call('alice', 'POST', base + '/submit', {'revision': task['revision'], 'version_id': first['id'], 'result': '配線測試通過'})
    assert submitted.status_code == 200
    task = submitted.json['task']
    approved = call('chief', 'POST', base + '/confirm', {'revision': task['revision'], 'submission_id': task['current_submission_id'], 'decision': 'approve', 'reason': '核對附件通過'})
    assert approved.status_code == 200
    task = approved.json['task']
    # Upload after confirmation is allowed; it is only a new draft.
    assert upload(clients, task, contents=b'new draft', document_id=first['document_id']).status_code == 200
    current = call('alice', 'GET', base).json['task']
    assert current['status'] == 'done' and current['adopted_version_id'] == first['id']
    files = call('alice', 'GET', base + '/files').json
    second = files['items'][1]
    assert second['number'] == 2 and files['submissions'][0]['version_id'] == first['id']
    adopted = call('alice', 'POST', base + '/adopt', {'revision': current['revision'], 'version_id': second['id'], 'reason': '更新實際施工版本'})
    assert adopted.status_code == 200 and adopted.json['task']['status'] == 'review'
    files = call('alice', 'GET', base + '/files').json
    assert files['submissions'][0]['decisions'][0]['decision'] == 'approve'
    assert files['submissions'][1]['decisions'] == []
    assert call('alice', 'GET', '/api/files/' + first['id'] + '/download').data == b'first evidence'


def test_self_confirmation_and_invisible_evidence_are_rejected(board):
    _, ids, clients, call = board
    task = make_work(call, 'chief', requires_confirmation=True)
    assert upload(clients, task, 'chief').status_code == 200
    base = '/api/tasks/' + task['id']
    version = call('chief', 'GET', base + '/files').json['items'][0]
    task = call('chief', 'POST', base + '/submit', {'revision': task['revision'], 'version_id': version['id'], 'result': '已完成'}).json['task']
    assert call('chief', 'POST', base + '/confirm', {'revision': task['revision'], 'submission_id': task['current_submission_id'], 'decision': 'approve', 'reason': '自己確認'}).status_code == 403
    restricted = make_work(call, requires_confirmation=True)
    assert upload(clients, restricted, readers=[ids['alice']]).status_code == 200
    base = '/api/tasks/' + restricted['id']
    version = call('alice', 'GET', base + '/files').json['items'][0]
    restricted = call('alice', 'POST', base + '/submit', {'revision': restricted['revision'], 'version_id': version['id'], 'result': '已完成'}).json['task']
    assert call('chief', 'GET', base + '/files').json['items'] == []
    assert call('chief', 'POST', base + '/confirm', {'revision': restricted['revision'], 'submission_id': restricted['current_submission_id'], 'decision': 'approve', 'reason': '無法讀卻確認'}).status_code == 404


def test_missing_blob_blocks_approval_but_preserves_return_history(board):
    app, _, clients, call = board
    task = make_work(call, requires_confirmation=True)
    base = '/api/tasks/' + task['id']
    upload(clients, task)
    version = call('alice', 'GET', base + '/files').json['items'][0]
    task = call('alice', 'POST', base + '/submit', {'revision': task['revision'], 'version_id': version['id'], 'result': '已驗證'}).json['task']
    with app.extensions['sessions']() as db:
        row = db.get(DocumentVersion, version['id'])
        # Deliberately remove only this test fixture blob to simulate restore loss.
        (Path(app.config['STORAGE_PATH']) / row.blob_key[:2] / row.blob_key).unlink()
    decision = {'revision': task['revision'], 'submission_id': task['current_submission_id'], 'decision': 'approve', 'reason': '核對'}
    assert call('chief', 'POST', base + '/confirm', decision).status_code == 409
    assert call('alice', 'GET', '/api/files/' + version['id'] + '/download').status_code == 409
    decision.update(decision='return', reason='附件不可用，請補正')
    assert call('chief', 'POST', base + '/confirm', decision).status_code == 200
    files = call('alice', 'GET', base + '/files').json
    assert files['submissions'][0]['version_id'] == version['id']
    assert files['submissions'][0]['decisions'][0]['decision'] == 'return'


def test_upload_retry_type_validation_and_no_cross_task_version_injection(board):
    _, _, clients, call = board
    first, second = make_work(call, requires_confirmation=True), make_work(call, requires_confirmation=True)
    for _ in range(2):
        assert upload(clients, first, key='upload-request-1').status_code == 200
    files = call('alice', 'GET', '/api/tasks/' + first['id'] + '/files').json['items']
    assert len(files) == 1
    assert upload(clients, first, filename='pretend.pdf').status_code == 400
    assert upload(clients, first, filename='../traverse.txt').status_code == 400
    assert upload(clients, first, filename='execute.exe').status_code == 400
    assert call('alice', 'POST', '/api/tasks/' + second['id'] + '/submit', {'revision': second['revision'], 'version_id': files[0]['id'], 'result': '借用別案'}).status_code == 404


def test_quarantine_is_not_a_passing_scan_and_blocks_download(board):
    app, _, clients, call = board
    # Missing actual scanner executable must not report clean.
    app.config['SCANNER_COMMAND'] = '["not-an-installed-scanner"]'
    task = make_work(call)
    assert upload(clients, task).status_code == 200
    file = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][0]
    assert file['scan_status'] == 'quarantined' and not file['available']
    assert call('alice', 'GET', '/api/files/' + file['id'] + '/download').status_code == 409
