from datetime import timedelta
from pathlib import Path
import pytest
from sqlalchemy import select
from conftest import make_work
from test_evidence import upload
from workboard.app import create_app
from workboard.backup import create_backup, restore_backup
from workboard.errors import DomainError
from workboard.models import Comment, DocumentVersion, LoginSession, Person, Task, utcnow


def expire(app, model, row_id):
    with app.extensions['sessions'].begin() as db:
        row = db.get(model, row_id)
        row.created_at = utcnow().replace(year=utcnow().year - 4)
        row.expires_at = utcnow() - timedelta(days=1)


def test_cleanup_is_selected_only_and_unexpired_items_fail_individually(board):
    app, _, clients, call = board
    task = make_work(call, title='不應向清理員洩漏此標題')
    upload(clients, task, contents=b'older file')
    first = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][0]
    upload(clients, task, contents=b'newer file', document_id=first['document_id'])
    second = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][1]
    expire(app, DocumentVersion, first['id'])
    candidates = call('cleaner', 'GET', '/api/cleanup')
    assert candidates.status_code == 200 and task['title'] not in candidates.text and first['filename'] not in candidates.text
    result = call('cleaner', 'POST', '/api/cleanup', {'action': 'clean', 'confirmed': True, 'reason': '到期處理',
             'items': [{'resource_type': 'version', 'resource_id': first['id'], 'revision': 0},
                       {'resource_type': 'version', 'resource_id': second['id'], 'revision': 0}]})
    assert result.status_code == 200
    assert result.json['results'][0]['status'] == 'done' and result.json['results'][1]['status'] == 'failed'
    assert call('alice', 'GET', '/api/files/' + first['id'] + '/download').status_code == 404
    assert call('alice', 'GET', '/api/files/' + second['id'] + '/download').data == b'newer file'
    assert call('alice', 'GET', '/api/cleanup').status_code == 403


def test_deferral_keeps_original_expiry_and_records_reason(board):
    app, _, _, call = board
    task = make_work(call)
    expire(app, Task, task['id'])
    row = next(r for r in call('cleaner', 'GET', '/api/cleanup').json['items'] if r['resource_id'] == task['id'])
    deferred = (utcnow() + timedelta(days=30)).date().isoformat()
    response = call('cleaner', 'POST', '/api/cleanup', {'action': 'defer', 'confirmed': True, 'reason': '保留複核期間', 'deferred_until': deferred,
                    'items': [{'resource_type': 'task', 'acknowledge_impacts': True, 'resource_id': task['id'], 'revision': row['revision']}]})
    assert response.json['results'][0]['status'] == 'deferred'
    current = next(r for r in call('cleaner', 'GET', '/api/cleanup').json['items'] if r['resource_id'] == task['id'])
    assert current['expires_at'] == row['expires_at'] and current['deferred_until'] == deferred
    assert call('cleaner', 'GET', '/api/cleanup/history').json['items'][0]['reason'] == '保留複核期間'
    selection = [{'resource_type': 'task', 'acknowledge_impacts': True, 'resource_id': task['id'], 'revision': current['revision']}]
    premature = call('cleaner', 'POST', '/api/cleanup', {'action': 'clean', 'confirmed': True, 'reason': '尚未解除', 'items': selection})
    assert premature.json['results'][0]['status'] == 'failed'
    resumed = call('cleaner', 'POST', '/api/cleanup', {'action': 'resume', 'confirmed': True, 'reason': '複核已提前結束', 'items': selection})
    assert resumed.json['results'][0]['status'] == 'pending'
    assert call('cleaner', 'POST', '/api/cleanup', {'action': 'clean', 'confirmed': True, 'reason': '重新確認清理', 'items': selection}).json['results'][0]['status'] == 'done'


def test_expiring_task_does_not_destroy_unselected_attachment(board):
    app, _, clients, call = board
    task = make_work(call)
    upload(clients, task)
    version = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][0]
    expire(app, Task, task['id'])
    row = next(r for r in call('cleaner', 'GET', '/api/cleanup').json['items'] if r['resource_id'] == task['id'])
    assert call('cleaner', 'POST', '/api/cleanup', {'action': 'clean', 'confirmed': True, 'reason': '清理工作內容',
                'items': [{'resource_type': 'task', 'acknowledge_impacts': True, 'resource_id': task['id'], 'revision': row['revision']}]}).json['results'][0]['status'] == 'done'
    assert call('alice', 'GET', '/api/tasks/' + task['id']).json['task']['title'] == '已清理工作'
    assert call('alice', 'GET', '/api/files/' + version['id'] + '/download').data == b'first evidence'


def test_backup_restore_matches_bytes_and_applies_current_revocations(board, tmp_path):
    app, ids, clients, call = board
    task = make_work(call, collaborators=[ids['bob']])
    upload(clients, task)
    version = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][0]
    backup_dir = tmp_path / 'recovery-point'
    manifest = create_backup(app.extensions['engine'], app.config['STORAGE_PATH'], backup_dir)
    assert manifest['formal_integration'] == 'NOT VERIFIED' and len(manifest['files']) == 1
    call('chief', 'POST', '/api/tasks/' + task['id'] + '/arrangement', {'revision': task['revision'], 'action': 'remove_collaborator', 'person_id': ids['bob'], 'reason': '備份後撤權'})
    target = tmp_path / 'restored'
    report = restore_backup(backup_dir, target, app.extensions['engine'])
    assert report['status'] == 'PASS' and report['files_checked'] == 1
    restored = create_app({'TESTING': True, 'APP_MODE': 'test', 'DATABASE_URL': 'sqlite:///' + (target / 'database.sqlite').as_posix(), 'STORAGE_PATH': str(target / 'attachments')})
    with restored.extensions['sessions']() as db:
        assert db.scalar(select(LoginSession)) is None
    def login_as(name):
        c = restored.test_client()
        csrf = c.get('/api/auth/status').json['csrf']
        assert c.post('/api/auth/login', json={'username': name}, headers={'X-CSRF-Token': csrf}).status_code == 200
        return c
    assert login_as('bob').get('/api/tasks/' + task['id']).status_code == 404
    assert login_as('alice').get('/api/files/' + version['id'] + '/download').data == b'first evidence'
    assert (target / 'code/workboard/app.py').exists()
    restored.extensions['engine'].dispose()


def test_restore_missing_file_fails_closed_and_never_overwrites_live(board, tmp_path):
    app, _, clients, call = board
    task = make_work(call)
    upload(clients, task)
    folder = tmp_path / 'backup'
    manifest = create_backup(app.extensions['engine'], app.config['STORAGE_PATH'], folder)
    item = manifest['files'][0]
    (folder / 'attachments' / item['key'][:2] / item['key']).unlink()
    target = tmp_path / 'missing-file-restore'
    with pytest.raises(DomainError, match='缺檔'):
        restore_backup(folder, target, app.extensions['engine'])
    restored = create_app({'TESTING': True, 'APP_MODE': 'test', 'DATABASE_URL': 'sqlite:///' + (target / 'database.sqlite').as_posix()})
    assert restored.test_client().get('/login').status_code == 503
    assert (target / 'RESTORE_BLOCKED').exists()
    assert call('alice', 'GET', '/api/tasks/' + task['id']).status_code == 200
    restored.extensions['engine'].dispose()


def test_restore_old_backup_does_not_resurrect_cleared_content(board, tmp_path):
    app, _, clients, call = board
    task = make_work(call, title='到期後不得復活的正文')
    upload(clients, task)
    backup_dir = tmp_path / 'older'
    create_backup(app.extensions['engine'], app.config['STORAGE_PATH'], backup_dir)
    expire(app, Task, task['id'])
    candidate = next(r for r in call('cleaner', 'GET', '/api/cleanup').json['items'] if r['resource_id'] == task['id'])
    call('cleaner', 'POST', '/api/cleanup', {'action': 'clean', 'confirmed': True, 'reason': '到期清理', 'items': [
        {'resource_type': 'task', 'acknowledge_impacts': True, 'resource_id': task['id'], 'revision': candidate['revision']} ]})
    target = tmp_path / 'restore-after-cleanup'
    restore_backup(backup_dir, target, app.extensions['engine'])
    restored = create_app({'TESTING': True, 'APP_MODE': 'test', 'DATABASE_URL': 'sqlite:///' + (target / 'database.sqlite').as_posix()})
    with restored.extensions['sessions']() as db:
        assert db.get(Task, task['id']).title == '已清理工作'
        from workboard.models import CleanupItem, CleanupHistory
        assert db.scalar(select(CleanupItem).where(CleanupItem.resource_id == task['id'])).status == 'done'
        history = list(db.scalars(select(CleanupHistory).where(CleanupHistory.resource_id == task['id'])))
        assert any(r.action == 'clean' and r.reason == '到期清理' for r in history)
        assert any(r.action == 'restore_apply' for r in history)
    restored.extensions['engine'].dispose()


def test_backup_copy_has_no_sessions_and_failed_early_restore_never_serves(board, tmp_path, monkeypatch):
    import sqlite3
    from workboard import backup
    app, _, _, call = board
    make_work(call)
    folder = tmp_path / 'safe-backup'
    create_backup(app.extensions['engine'], app.config['STORAGE_PATH'], folder)
    with sqlite3.connect(folder / 'database.sqlite') as copy:
        assert copy.execute("SELECT value FROM sequences WHERE key='restore_ready'").fetchone() == (0,)
        assert copy.execute('SELECT count(*) FROM login_sessions').fetchone() == (0,)
    def fail_before_database_open(*_):
        raise RuntimeError('simulated interruption immediately after copy')
    monkeypatch.setattr(backup, 'database', fail_before_database_open)
    target = tmp_path / 'interrupted-restore'
    with pytest.raises(RuntimeError, match='interruption'):
        restore_backup(folder, target, app.extensions['engine'])
    recovered = create_app({'TESTING': True, 'APP_MODE': 'test', 'DATABASE_URL': 'sqlite:///' + (target / 'database.sqlite').as_posix()})
    assert recovered.test_client().get('/login').status_code == 503
    assert recovered.test_client().get('/api/health').status_code == 503
    recovered.extensions['engine'].dispose()
    assert call('alice', 'GET', '/api/tasks').status_code == 200


def test_restore_replays_current_quarantine_instead_of_old_clean_result(board, tmp_path):
    app, _, clients, call = board
    task = make_work(call)
    upload(clients, task)
    version = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][0]
    folder = tmp_path / 'before-quarantine'
    create_backup(app.extensions['engine'], app.config['STORAGE_PATH'], folder)
    with app.extensions['sessions'].begin() as db:
        db.get(DocumentVersion, version['id']).scan_status = 'infected'
    target = tmp_path / 'after-quarantine'
    restore_backup(folder, target, app.extensions['engine'])
    recovered = create_app({'TESTING': True, 'APP_MODE': 'test', 'DATABASE_URL': 'sqlite:///' + (target / 'database.sqlite').as_posix(), 'STORAGE_PATH': str(target / 'attachments')})
    c = recovered.test_client()
    token = c.get('/api/auth/status').json['csrf']
    assert c.post('/api/auth/login', json={'username': 'alice'}, headers={'X-CSRF-Token': token}).status_code == 200
    assert c.get('/api/files/' + version['id'] + '/download').status_code == 409
    recovered.extensions['engine'].dispose()


def test_restore_holds_source_write_barrier_until_copy_verified(board, tmp_path, monkeypatch):
    import sqlite3
    from workboard import backup
    app, _, _, call = board
    make_work(call)
    folder = tmp_path / 'locked-backup'
    create_backup(app.extensions['engine'], app.config['STORAGE_PATH'], folder)
    original = backup.shutil.copyfile
    observed = []
    def copy_while_revocation_attempted(source, destination, **kwargs):
        if Path(destination).name == 'database.sqlite':
            # A second real DB connection must not commit a concurrent role
            # revocation after the restore ledger was observed.
            with sqlite3.connect(app.extensions['engine'].url.database, timeout=0.05) as live:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    live.execute("UPDATE people SET active=0 WHERE username='alice'")
            observed.append(True)
        return original(source, destination, **kwargs)
    monkeypatch.setattr(backup.shutil, 'copyfile', copy_while_revocation_attempted)
    restore_backup(folder, tmp_path / 'barrier-restored', app.extensions['engine'])
    assert observed == [True]


def test_cleaned_work_cannot_be_repopulated_by_comments_or_subtasks(board):
    app, _, _, call = board
    task = make_work(call)
    expire(app, Task, task['id'])
    row = next(r for r in call('cleaner', 'GET', '/api/cleanup').json['items'] if r['resource_id'] == task['id'])
    assert call('cleaner', 'POST', '/api/cleanup', {'action': 'clean', 'confirmed': True, 'reason': '到期處理',
                'items': [{'resource_type': 'task', 'acknowledge_impacts': True, 'resource_id': task['id'], 'revision': row['revision']}]}).json['results'][0]['status'] == 'done'
    assert call('alice', 'POST', '/api/tasks/' + task['id'] + '/comments', {'body': '不得重新填入'}).status_code == 409
    assert call('alice', 'POST', '/api/tasks', {'title': '不得新增', 'parent_id': task['id']}).status_code == 409
def test_active_template_still_expires_at_creation_plus_three_years_but_requires_ack(board):
    from test_governance import template
    from workboard.models import Template
    app, _, _, call = board
    t = template(call)
    expire(app, Template, t['id'])
    item = next(r for r in call('cleaner', 'GET', '/api/cleanup').json['items'] if r['resource_id'] == t['id'])
    assert item['impacts'] == ['active_template']
    selection = {'resource_type': 'template', 'resource_id': t['id'], 'revision': item['revision']}
    body = {'action': 'clean', 'confirmed': True, 'reason': '已確認使用影響', 'items': [selection]}
    assert call('cleaner', 'POST', '/api/cleanup', body).json['results'][0]['status'] == 'failed'
    selection['acknowledge_impacts'] = True
    assert call('cleaner', 'POST', '/api/cleanup', body).json['results'][0]['status'] == 'done'
    with app.extensions['sessions']() as db:
        row = db.get(Template, t['id'])
        assert row.expires_at.isoformat() + 'Z' == item['expires_at'] and row.status == 'retired'


def test_cleaning_two_template_families_does_not_collide(board):
    from workboard.models import Template
    app, _, _, call = board
    ids = []
    for name in ['採購範本', '維運範本']:
        row = call('admin', 'POST', '/api/templates', {'name': name, 'phases': ['驗收']}).json['template']
        assert row['version'] == 1
        expire(app, Template, row['id'])
        ids.append(row['id'])
    items = [r for r in call('cleaner', 'GET', '/api/cleanup').json['items'] if r['resource_id'] in ids]
    response = call('cleaner', 'POST', '/api/cleanup', {'action': 'clean', 'confirmed': True, 'reason': '到期範本清理',
        'items': [{k: r[k] for k in ['resource_type', 'resource_id', 'revision']} for r in items]})
    assert response.status_code == 200
    assert [r['status'] for r in response.json['results']] == ['done', 'done']
    with app.extensions['sessions']() as db:
        rows = [db.get(Template, i) for i in ids]
        assert rows[0].name != rows[1].name and all(r.status == 'retired' for r in rows)


def test_restore_does_not_resurrect_withdrawn_comment(board, tmp_path):
    app, _, _, call = board
    task = make_work(call)
    path = '/api/tasks/' + task['id']
    for text in ['誤貼的內容', '保留的內容']:
        assert call('alice', 'POST', path + '/comments', {'body': text}).status_code == 200
    withdrawn = next(c for c in call('alice', 'GET', path).json['comments'] if c['body'] == '誤貼的內容')
    folder = tmp_path / 'before-withdraw'
    create_backup(app.extensions['engine'], app.config['STORAGE_PATH'], folder)
    assert call('alice', 'PATCH', path + '/comments/' + withdrawn['id'], {'revision': withdrawn['revision'], 'withdraw': True}).status_code == 200
    target = tmp_path / 'after-withdraw'
    restore_backup(folder, target, app.extensions['engine'])
    recovered = create_app({'TESTING': True, 'APP_MODE': 'test', 'DATABASE_URL': 'sqlite:///' + (target / 'database.sqlite').as_posix()})
    client = recovered.test_client()
    token = client.get('/api/auth/status').json['csrf']
    assert client.post('/api/auth/login', json={'username': 'alice'}, headers={'X-CSRF-Token': token}).status_code == 200
    response = client.get(path)
    assert '誤貼的內容' not in response.text
    assert any(row['body'] == '保留的內容' for row in response.json['comments'])
    assert next(row for row in response.json['comments'] if row['id'] == withdrawn['id'])['withdrawn']
    recovered.extensions['engine'].dispose()


@pytest.mark.parametrize('kind,flag', [('task', 'active_task'), ('project', 'active_project'),
                                    ('coordination', 'open_coordination'), ('allocation', 'future_allocation'),
                                    ('capacity', 'future_allocation')])
def test_active_expired_records_need_impact_ack_without_leaking_contents(board, kind, flag):
    from datetime import date
    from workboard.models import Allocation, Capacity, Coordination, Project
    from test_projects import project
    app, ids, _, call = board
    task = make_work(call, title='不可洩漏的內容', collaborators=[ids['bob']])
    if kind == 'task':
        model, row_id = Task, task['id']
    elif kind == 'project':
        model, row_id = Project, project(call)['id']
    else:
        with app.extensions['sessions'].begin() as db:
            if kind == 'coordination':
                row = Coordination(resource_type='task', resource_id=task['id'], creator_id=ids['alice'],
                                   assignee_id=ids['bob'], subject='不可洩漏的內容', follow_on=date(2030, 1, 1))
            elif kind == 'capacity':
                row = Capacity(person_id=ids['alice'], week=date(2030, 1, 7), hours=40)
            else:
                row = Allocation(task_id=task['id'], person_id=ids['alice'], week=date(2030, 1, 7), hours=5, remaining=10)
            db.add(row)
            db.flush()
            model, row_id = type(row), row.id
    expire(app, model, row_id)
    response = call('cleaner', 'GET', '/api/cleanup')
    assert '不可洩漏的內容' not in response.text
    item = next(r for r in response.json['items'] if r['resource_id'] == row_id)
    assert item['impacts'] == [flag]
    selection = {k: item[k] for k in ['resource_type', 'resource_id', 'revision']}
    body = {'action': 'clean', 'confirmed': True, 'reason': '清理前核對使用影響', 'items': [selection]}
    result = call('cleaner', 'POST', '/api/cleanup', body).json['results'][0]
    assert result['status'] == 'failed' and result['failure_code'] == 'active_record_acknowledgement_required'
    selection['acknowledge_impacts'] = True
    assert call('cleaner', 'POST', '/api/cleanup', body).json['results'][0]['status'] == 'done'
    with app.extensions['sessions']() as db:
        row = db.get(model, row_id)
        assert row.expires_at.isoformat() + 'Z' == item['expires_at']
        if kind == 'capacity':
            assert row.hours is None


def test_expired_adopted_version_warns_and_does_not_expose_filename(board):
    app, _, clients, call = board
    task = make_work(call)
    upload(clients, task, filename='不可顯示的成果.txt')
    version = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][0]
    assert call('alice', 'POST', '/api/tasks/' + task['id'] + '/adopt', {'revision': task['revision'], 'version_id': version['id'], 'reason': '採用成果'}).status_code == 200
    expire(app, DocumentVersion, version['id'])
    response = call('cleaner', 'GET', '/api/cleanup')
    assert version['filename'] not in response.text
    item = next(r for r in response.json['items'] if r['resource_id'] == version['id'])
    assert item['impacts'] == ['adopted_evidence']
    selection = {k: item[k] for k in ['resource_type', 'resource_id', 'revision']}
    body = {'action': 'clean', 'confirmed': True, 'reason': '到期成果核對', 'items': [selection]}
    assert call('cleaner', 'POST', '/api/cleanup', body).json['results'][0]['status'] == 'failed'
    selection['acknowledge_impacts'] = True
    assert call('cleaner', 'POST', '/api/cleanup', body).json['results'][0]['status'] == 'done'


def test_expired_grant_is_revoked_and_cannot_be_reactivated_as_cleaned_record(board):
    from workboard.models import CapabilityGrant
    app, ids, _, call = board
    task = make_work(call, collaborators=[ids['bob']])
    path = '/api/grants/task/' + task['id']
    grant = {'person_id': ids['bob'], 'capability': 'cancel', 'reason': '授權取消'}
    assert call('chief', 'POST', path, grant).status_code == 200
    with app.extensions['sessions']() as db:
        row_id = db.scalar(select(CapabilityGrant.id).where(CapabilityGrant.scope_id == task['id']))
    expire(app, CapabilityGrant, row_id)
    item = next(r for r in call('cleaner', 'GET', '/api/cleanup').json['items'] if r['resource_id'] == row_id)
    assert item['impacts'] == ['active_grant']
    selection = {k: item[k] for k in ['resource_type', 'resource_id', 'revision']}
    body = {'action': 'clean', 'confirmed': True, 'reason': '到期授權核對', 'items': [selection]}
    assert call('cleaner', 'POST', '/api/cleanup', body).json['results'][0]['status'] == 'failed'
    selection['acknowledge_impacts'] = True
    assert call('cleaner', 'POST', '/api/cleanup', body).json['results'][0]['status'] == 'done'
    assert call('bob', 'POST', '/api/tasks/' + task['id'] + '/control', {'revision': task['revision'], 'action': 'cancel', 'reason': '已過期不可取消'}).status_code == 403
    assert call('chief', 'POST', path, grant).status_code == 409
    assert call('chief', 'POST', path, grant | {'revoke': True}).status_code == 200
    assert call('chief', 'POST', path, grant).status_code == 200


def test_conflicting_cleanup_item_rolls_back_only_its_savepoint(board, monkeypatch):
    from workboard import retention
    from workboard.models import Template
    app, _, _, call = board
    rows = [call('admin', 'POST', '/api/templates', {'name': n, 'phases': ['驗收']}).json['template']
            for n in ['保留唯一鍵', '衝突項', '正常項']]
    for row in rows[1:]:
        expire(app, Template, row['id'])
    original = retention.clear_content
    def conflicting(row):
        if isinstance(row, Template) and row.id == rows[1]['id']:
            row.name = '保留唯一鍵'
        else:
            original(row)
    monkeypatch.setattr(retention, 'clear_content', conflicting)
    items = call('cleaner', 'GET', '/api/cleanup').json['items']
    response = call('cleaner', 'POST', '/api/cleanup', {'action': 'clean', 'confirmed': True, 'reason': '逐筆隔離衝突',
        'items': [{k: row[k] for k in ['resource_type', 'resource_id', 'revision']} for row in items]})
    assert response.status_code == 200
    result = {r['resource_id']: r for r in response.json['results']}
    assert result[rows[1]['id']]['failure_code'] == 'conflict'
    assert result[rows[2]['id']]['status'] == 'done'
    history = call('cleaner', 'GET', '/api/cleanup/history').json['items']
    assert any(r['resource_id'] == rows[1]['id'] and r['status'] == 'failed' for r in history)
    assert any(r['resource_id'] == rows[2]['id'] and r['status'] == 'done' for r in history)
