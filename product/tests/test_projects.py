from conftest import make_work
from test_evidence import upload


def project(call, who='alice', **data):
    r = call(who, 'POST', '/api/projects', {'name': '機房搬遷', 'goal': '完成搬遷與服務驗證', **data})
    assert r.status_code == 200, r.json
    return r.json['project']


def test_nested_decomposition_preserves_fixed_ids_and_rejects_reparenting(board):
    _, ids, _, call = board
    root = project(call, members=[ids['bob']])
    sub = project(call, parent_id=root['id'], name='內部機房')
    task = make_work(call, project_id=sub['id'])
    child = make_work(call, project_id=sub['id'], parent_id=task['id'], title='配線')
    assert root['code'].startswith('proj2026') and child['code'].startswith('work2026')
    assert call('bob', 'GET', '/api/projects/' + root['id']).status_code == 200
    assert call('bob', 'GET', '/api/tasks/' + task['id']).status_code == 404
    renamed = call('bob', 'PATCH', '/api/projects/' + root['id'], {'revision': root['revision'], 'name': '主機房搬遷', 'reason': '調整名稱'})
    assert renamed.status_code == 200 and renamed.json['project']['code'] == root['code']
    assert call('alice', 'PATCH', '/api/projects/' + sub['id'], {'revision': sub['revision'], 'parent_id': None, 'reason': '搬移'}).status_code == 400
    assert call('alice', 'POST', '/api/tasks', {'title': '跨案子工作', 'parent_id': task['id'], 'project_id': root['id']}).status_code == 400


def test_leaf_plan_changes_keep_commitment_and_roll_up_only_visible_work(board):
    _, ids, _, call = board
    root = project(call, members=[ids['bob']])
    task = make_work(call, project_id=root['id'], target_on='2026-12-01')
    base = '/api/tasks/' + task['id']
    result = call('alice', 'POST', base + '/plan', {'revision': task['revision'], 'planned_start': '2026-11-24', 'planned_end': '2026-11-26', 'reason': '排定窗口'})
    assert result.status_code == 200 and result.json['task']['target_on'] == '2026-12-01'
    assert call('alice', 'GET', '/api/projects/' + root['id']).json['project']['planned_start'] == '2026-11-24'
    bob = call('bob', 'GET', '/api/projects/' + root['id']).json['project']
    assert bob['planned_start'] is None and bob['visible_total'] == 0
    make_work(call, project_id=root['id'], parent_id=task['id'])
    assert call('alice', 'POST', base + '/plan', {'revision': result.json['task']['revision'], 'planned_start': '2026-12-01', 'planned_end': '2026-12-02', 'reason': '改整組'}).status_code == 409


def test_unfinished_descendants_block_closure_without_leaking_names(board):
    _, ids, _, call = board
    root = project(call)
    hidden = make_work(call, project_id=root['id'], owner_id=ids['bob'], assignment='proposed', title='不公開的工作標題')
    call('bob', 'POST', '/api/tasks/' + hidden['id'] + '/arrangement', {'revision': hidden['revision'], 'action': 'accept'})
    result = call('alice', 'POST', '/api/projects/' + root['id'] + '/actions', {'revision': root['revision'], 'action': 'submit', 'reason': '提出', 'result': '完成'})
    assert result.status_code == 409 and '不公開的工作標題' not in result.text


def test_parent_schedule_uses_visible_leaf_dates_and_ignores_stale_parent_plan(board):
    app, ids, _, call = board
    root = project(call)
    parent = make_work(call, project_id=root['id'], collaborators=[ids['bob']])
    path = '/api/tasks/' + parent['id']
    assert call('alice', 'POST', path + '/plan', {'revision': parent['revision'], 'planned_start': '2026-01-01', 'planned_end': '2026-12-31', 'reason': '初始計畫'}).status_code == 200
    child = make_work(call, project_id=root['id'], parent_id=parent['id'], title='末層工作')
    child_path = '/api/tasks/' + child['id']
    assert call('alice', 'POST', child_path + '/plan', {'revision': child['revision'], 'planned_start': '2026-11-24', 'planned_end': '2026-11-26', 'reason': '分解排程'}).status_code == 200
    for path in [path, '/api/projects/' + root['id']]:
        result = call('alice', 'GET', path).json
        row = result.get('task') or result['project']
        assert (row['planned_start'], row['planned_end']) == ('2026-11-24', '2026-11-26')
    # Bob can read the parent, but the child was not shared with him.
    parent_view = call('bob', 'GET', '/api/tasks/' + parent['id']).json['task']
    assert parent_view['planned_start'] is None and parent_view['planned_end'] is None
    assert call('bob', 'GET', child_path).status_code == 404
    from workboard.models import Task
    with app.extensions['sessions'].begin() as db:
        db.get(Task, child['id']).cancelled = True
    assert call('alice', 'GET', '/api/tasks/' + parent['id']).json['task']['planned_start'] is None


def test_project_submission_snapshot_and_closed_parent_guards(board):
    _, ids, clients, call = board
    root = project(call)
    task = make_work(call, project_id=root['id'])
    upload(clients, task)
    base = '/api/tasks/' + task['id']
    version = call('alice', 'GET', base + '/files').json['items'][0]
    task = call('alice', 'POST', base + '/adopt', {'revision': task['revision'], 'version_id': version['id'], 'reason': '採用成果'}).json['task']
    task = call('alice', 'POST', base + '/status', {'revision': task['revision'], 'status': 'done', 'result': '搬遷驗證完成'}).json['task']
    path = '/api/projects/' + root['id']
    submitted = call('alice', 'POST', path + '/actions', {'revision': root['revision'], 'action': 'submit', 'reason': '提出成果', 'result': '完成搬遷', 'evidence_task_id': task['id'], 'version_id': version['id']})
    assert submitted.status_code == 200, submitted.json
    root = submitted.json['project']
    confirmed = call('chief', 'POST', path + '/actions', {'revision': root['revision'], 'action': 'confirm', 'submission_id': root['current_submission_id'], 'decision': 'approve', 'reason': '已核對完成交付'})
    assert confirmed.status_code == 200, confirmed.json
    root = confirmed.json['project']
    assert root['status'] == 'done'
    assert call('alice', 'POST', '/api/tasks', {'title': '偷加子項', 'project_id': root['id']}).status_code == 409
    assert call('alice', 'POST', base + '/status', {'revision': task['revision'], 'status': 'doing', 'reason': '修改', 'next_action': '檢查', 'follow_on': '2026-11-24'}).status_code == 409
    assert call('alice', 'POST', path + '/actions', {'revision': root['revision'], 'action': 'reopen', 'reason': '補驗'}).status_code == 200
    assert call('alice', 'POST', base + '/status', {'revision': task['revision'], 'status': 'doing', 'reason': '修改', 'next_action': '檢查', 'follow_on': '2026-11-24'}).status_code == 200
