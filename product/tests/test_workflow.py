from datetime import date
from sqlalchemy import select
from conftest import make_work
from workboard.models import Event, Task


def test_search_detail_and_counts_exclude_other_peoples_work(board):
    app, ids, clients, call = board
    task = make_work(call)
    assert call('outsider', 'GET', '/api/tasks').json['items'] == []
    assert call('outsider', 'GET', '/api/tasks?q=機房').json['items'] == []
    assert call('outsider', 'GET', '/api/tasks/' + task['id']).status_code == 404
    assert len(call('chief', 'GET', '/api/tasks').json['items']) == 1
    assert call('admin', 'GET', '/api/tasks').json['items'] == []


def test_create_and_comment_retries_are_idempotent(board):
    app, ids, clients, call = board
    data = {'title': '斷電檢討'}
    first = call('alice', 'POST', '/api/tasks', data, key='same-create-key')
    second = call('alice', 'POST', '/api/tasks', data, key='same-create-key')
    assert first.json['task']['id'] == second.json['task']['id']
    assert second.json['replayed']
    assert call('alice', 'POST', '/api/tasks', {'title': '另一件'}, key='same-create-key').status_code == 409
    task = first.json['task']
    for _ in range(2):
        assert call('alice', 'POST', f"/api/tasks/{task['id']}/comments", {'body': '已與廠商聯繫'}, key='same-comment-key').status_code == 200
    detail = call('alice', 'GET', '/api/tasks/' + task['id']).json
    assert len(detail['comments']) == 1
    assert detail['task']['revision'] == task['revision']
    assert detail['task']['status'] == 'todo'


def test_collaborator_can_edit_but_cannot_cancel_or_complete(board):
    _, ids, _, call = board
    task = make_work(call, collaborators=[ids['bob']])
    path = '/api/tasks/' + task['id']
    r = call('bob', 'PATCH', path, {'revision': task['revision'], 'result': '已完成配線檢查'})
    assert r.status_code == 200
    updated = r.json['task']
    assert call('alice', 'PATCH', path, {'revision': task['revision'], 'result': '舊畫面覆寫'}).status_code == 409
    assert call('bob', 'POST', path + '/control', {'revision': updated['revision'], 'action': 'cancel', 'reason': '不做'}).status_code == 403
    assert call('bob', 'POST', path + '/status', {'revision': updated['revision'], 'status': 'done'}).status_code == 403
    assert call('alice', 'POST', path + '/status', {'revision': updated['revision'], 'status': 'done'}).status_code == 200


def test_proposal_decline_and_reproposal_retains_original_coordinator(board):
    _, ids, _, call = board
    task = make_work(call, owner_id=ids['bob'], assignment='proposed')
    path = '/api/tasks/' + task['id']
    assert task['owner_id'] is None
    assert call('bob', 'POST', path + '/status', {'revision': task['revision'], 'status': 'doing'}).status_code == 409
    declined = call('bob', 'POST', path + '/arrangement', {'revision': task['revision'], 'action': 'decline', 'reason': '另有安排'})
    assert declined.status_code == 200 and declined.json['access_ended']
    assert call('bob', 'GET', path).status_code == 404
    current = call('alice', 'GET', path).json['task']
    assert current['assignment'] == 'unassigned'
    proposed = call('alice', 'POST', path + '/arrangement', {'revision': current['revision'], 'action': 'propose', 'person_id': ids['outsider'], 'reason': '重新協調'})
    accepted = call('outsider', 'POST', path + '/arrangement', {'revision': proposed.json['task']['revision'], 'action': 'accept'})
    assert accepted.status_code == 200
    assert accepted.json['task']['owner_id'] == ids['outsider']
    assert call('alice', 'GET', path).status_code == 404


def test_assigned_work_does_not_transfer_cancel_authority(board):
    _, ids, _, call = board
    assert call('alice', 'POST', '/api/tasks', {'title': '派給B', 'owner_id': ids['bob'], 'assignment': 'assigned'}).status_code == 403
    task = make_work(call, 'chief', owner_id=ids['bob'], assignment='assigned', source='assigned')
    path = '/api/tasks/' + task['id']
    acknowledged = call('bob', 'POST', path + '/arrangement', {'revision': task['revision'], 'action': 'acknowledge'})
    assert acknowledged.status_code == 200
    assert call('bob', 'POST', path + '/control', {'revision': acknowledged.json['task']['revision'], 'action': 'cancel', 'reason': '不做'}).status_code == 403


def test_waiting_requires_tracking_and_cannot_skip_to_done(board):
    _, _, _, call = board
    task = make_work(call)
    path = '/api/tasks/' + task['id']
    assert call('alice', 'POST', path + '/status', {'revision': task['revision'], 'status': 'doing'}).status_code == 400
    doing = call('alice', 'POST', path + '/status', {'revision': task['revision'], 'status': 'doing', 'next_action': '量測電源', 'follow_on': '2026-11-24'}).json['task']
    assert call('alice', 'POST', path + '/status', {'revision': doing['revision'], 'status': 'waiting'}).status_code == 400
    waiting = call('alice', 'POST', path + '/status', {'revision': doing['revision'], 'status': 'waiting', 'wait_for': '廠商提供測試報告'}).json['task']
    assert call('alice', 'POST', path + '/status', {'revision': waiting['revision'], 'status': 'done', 'result': '口頭回覆完成'}).status_code == 409
    assert call('alice', 'POST', path + '/status', {'revision': waiting['revision'], 'status': 'doing'}).status_code == 409
    assert call('alice', 'POST', path + '/status', {'revision': waiting['revision'], 'status': 'doing', 'reason': '已收到報告'}).status_code == 200


def test_confirmation_floor_and_forged_fields_are_rejected(board):
    _, _, _, call = board
    task = make_work(call, requires_confirmation=True)
    path = '/api/tasks/' + task['id']
    assert call('alice', 'POST', path + '/status', {'revision': task['revision'], 'status': 'done', 'result': '完成'}).status_code == 409
    for forbidden in ['requires_confirmation', 'owner_id', 'project_id', 'parent_id', 'created_at']:
        assert call('alice', 'PATCH', path, {'revision': task['revision'], forbidden: False}).status_code == 400


def test_commitment_changes_have_reasons_and_do_not_reset_retention(board):
    app, ids, _, call = board
    task = make_work(call)
    path = '/api/tasks/' + task['id']
    assert call('alice', 'POST', path + '/commitment', {'revision': task['revision'], 'priority': 1, 'reason': '緊急'}).status_code == 403
    assert call('chief', 'POST', path + '/commitment', {'revision': task['revision'], 'priority': 1}).status_code == 400
    result = call('chief', 'POST', path + '/commitment', {'revision': task['revision'], 'priority': 1, 'target_on': '2026-11-24', 'reason': '配合稽核'}).json['task']
    assert result['created_at'] == task['created_at'] and result['expires_at'] == task['expires_at']
    events = call('alice', 'GET', path).json['events']
    change = next(e for e in events if e['action'] == 'commitment')
    assert change['before']['priority'] == 2 and change['after']['priority'] == 1


def test_secret_list_overrides_chief_and_has_its_own_revision(board):
    _, ids, _, call = board
    task = make_work(call, collaborators=[ids['bob']])
    path = '/api/tasks/' + task['id']
    body = {'revision': task['access_revision'], 'assignment_revision': task['revision'], 'kind': 'confidential', 'confidential': True, 'readers': [ids['chief']], 'reason': '機密檢討'}
    assert call('chief', 'PUT', path + '/access', body).status_code == 409
    body['acknowledge_excluded'] = True
    assert call('chief', 'PUT', path + '/access', body).status_code == 200
    for person in ['alice', 'bob', 'director', 'admin']:
        assert call(person, 'GET', path).status_code == 404
    current = call('chief', 'GET', path).json['task']
    assert current['revision'] == task['revision'] and current['access_revision'] > task['access_revision']
    body.update(revision=current['access_revision'], readers=[ids['alice']])
    assert call('chief', 'PUT', path + '/access', body).status_code == 409


def test_revoked_collaborator_loses_cached_idempotency_response(board):
    _, ids, _, call = board
    task = make_work(call, collaborators=[ids['bob']])
    path = '/api/tasks/' + task['id']
    data = {'body': '先補充'}
    assert call('bob', 'POST', path + '/comments', data, key='retained-comment').status_code == 200
    assert call('chief', 'POST', path + '/arrangement', {'revision': task['revision'], 'action': 'remove_collaborator', 'person_id': ids['bob'], 'reason': '結束協作'}).status_code == 200
    assert call('bob', 'POST', path + '/comments', data, key='retained-comment').status_code == 404


def test_comment_author_and_cancelled_history_are_preserved(board):
    _, ids, _, call = board
    task = make_work(call, collaborators=[ids['bob']])
    path = '/api/tasks/' + task['id']
    call('alice', 'POST', path + '/comments', {'body': '原始紀錄'})
    row = call('alice', 'GET', path).json['comments'][0]
    assert call('bob', 'PATCH', path + '/comments/' + row['id'], {'revision': row['revision'], 'body': '改別人的'}).status_code == 403
    assert call('alice', 'PATCH', path + '/comments/' + row['id'], {'revision': row['revision'], 'withdraw': True}).status_code == 200
    assert call('alice', 'POST', path + '/control', {'revision': task['revision'], 'action': 'cancel', 'reason': '停止辦理'}).status_code == 200
    assert call('alice', 'GET', '/api/tasks').json['items'] == []
    detail = call('alice', 'GET', path).json
    assert detail['task']['status'] == 'todo' and detail['task']['cancelled']
    assert detail['comments'][0]['withdrawn']


def test_admin_disables_sessions_without_reading_business_content(board):
    _, ids, _, call = board
    task = make_work(call)
    users = call('admin', 'GET', '/api/admin/people').json['items']
    alice = next(p for p in users if p['id'] == ids['alice'])
    assert call('chief', 'GET', '/api/admin/people').status_code == 403
    result = call('admin', 'PATCH', '/api/admin/people/' + ids['alice'], {'revision': alice['revision'], 'active': False, 'reason': '停止使用'})
    assert result.status_code == 200
    assert call('alice', 'GET', '/api/tasks').status_code == 401
    assert call('admin', 'GET', '/api/tasks/' + task['id']).status_code == 404


def test_failed_logins_count_even_when_authentication_rolls_back(board):
    app, _, _, _ = board
    client = app.test_client()
    for index in range(21):
        token = client.get('/api/auth/status').json['csrf']
        result = client.post('/api/auth/login', json={'username': 'unknown'}, headers={'X-CSRF-Token': token})
        assert result.status_code == (401 if index < 20 else 429)
