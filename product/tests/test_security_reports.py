import io
from datetime import date
from sqlalchemy import select
from conftest import make_work
from test_projects import project
from workboard.models import CapabilityGrant, Coordination, Person, SecurityEvent, Task
from workboard.policy import can_manage, can_read, global_capability


def test_single_task_grant_does_not_authorize_another_project_task(board):
    app, ids, _, call = board
    root = project(call)
    first = make_work(call, project_id=root['id'], collaborators=[ids['bob']])
    second = make_work(call, project_id=root['id'], collaborators=[ids['bob']])
    r = call('chief', 'POST', '/api/grants/task/' + first['id'], {'person_id': ids['bob'], 'capability': 'cancel', 'reason': '只授權第一項'})
    assert r.status_code == 200
    assert call('bob', 'POST', '/api/tasks/' + first['id'] + '/control', {'revision': first['revision'], 'action': 'cancel', 'reason': '依授權取消'}).status_code == 200
    assert call('bob', 'POST', '/api/tasks/' + second['id'] + '/control', {'revision': second['revision'], 'action': 'cancel', 'reason': '嘗試跨工作'}).status_code == 403


def test_explicit_dual_role_allowed_but_maintenance_role_itself_has_no_business_read(board):
    app, ids, _, call = board
    task = make_work(call)
    with app.extensions['sessions'].begin() as db:
        admin = db.get(Person, ids['admin'])
        assert not can_read(db, admin, db.get(Task, task['id']))
        # Baseline explicitly allows one person to hold distinct roles.
        # This is an intentional directory authorization, not an admin bypass.
        admin.roles_json = '["maintenance","chief"]'
        db.flush()
        assert can_read(db, admin, db.get(Task, task['id']))
        assert global_capability(admin, 'users') and global_capability(admin, 'department_view')


def test_admin_cannot_change_own_roles_and_secret_list_rejects_nonworker(board):
    _, ids, _, call = board
    admin = next(p for p in call('admin', 'GET', '/api/admin/people').json['items'] if p['id'] == ids['admin'])
    assert call('admin', 'PATCH', '/api/admin/people/' + ids['admin'], {'revision': admin['revision'], 'roles': ['maintenance', 'chief'], 'reason': '自行提升'}).status_code == 409
    task = make_work(call)
    r = call('chief', 'PUT', '/api/tasks/' + task['id'] + '/access', {'revision': task['access_revision'], 'kind': 'confidential', 'confidential': True,
             'readers': [ids['admin']], 'reason': '錯誤管理者', 'acknowledge_excluded': True, 'assignment_revision': task['revision']})
    assert r.status_code == 400


def test_coordination_does_not_create_read_authority_and_revocation_hides_it(board):
    app, ids, _, call = board
    task = make_work(call, collaborators=[ids['bob']])
    assert call('outsider', 'POST', '/api/coordinations', {'resource_type': 'task', 'resource_id': task['id'], 'assignee_id': ids['bob'], 'subject': '自助取得', 'follow_on': '2026-11-25'}).status_code == 404
    # Even a malformed preexisting row is not a read-grant back door.
    with app.extensions['sessions'].begin() as db:
        db.add(Coordination(resource_type='task', resource_id=task['id'], creator_id=ids['outsider'], assignee_id=ids['outsider'], subject='惡意列', follow_on=date(2026,11,25)))
    assert call('outsider', 'GET', '/api/tasks/' + task['id']).status_code == 404
    created = call('alice', 'POST', '/api/coordinations', {'resource_type': 'task', 'resource_id': task['id'], 'assignee_id': ids['bob'], 'subject': '請協助聯繫', 'follow_on': '2026-11-25'})
    assert created.status_code == 200
    call('chief', 'POST', '/api/tasks/' + task['id'] + '/arrangement', {'revision': task['revision'], 'action': 'remove_collaborator', 'person_id': ids['bob'], 'reason': '停止協作'})
    assert call('bob', 'GET', '/api/coordinations').json['items'] == []
    assert call('bob', 'GET', '/api/tasks/' + task['id']).status_code == 404


def test_security_failures_survive_rollback_without_request_body(board):
    app, ids, clients, call = board
    task = make_work(call, title='機密內容不得寫進拒絕紀錄')
    assert call('outsider', 'GET', '/api/tasks/' + task['id']).status_code == 404
    with app.extensions['sessions']() as db:
        record = db.scalar(select(SecurityEvent).where(SecurityEvent.actor_id == ids['outsider'], SecurityEvent.outcome == 404))
        assert record is not None and record.route == 'api.get_task'
        assert '機密內容' not in record.code and task['id'] not in record.route


def test_weekly_and_exports_never_include_hidden_tasks_and_escape_spreadsheet_formulas(board):
    _, _, _, call = board
    task = make_work(call, title='=1+2')
    hidden = make_work(call, 'bob', title='不可見的B工作')
    assert hidden['id'] not in str(call('alice', 'GET', '/api/weekly').json)
    csv = call('alice', 'GET', '/api/exports/tasks.csv').data.decode('utf-8-sig')
    assert "'=1+2" in csv and '不可見的B工作' not in csv
    md = call('alice', 'GET', '/api/exports/tasks.md').data.decode()
    assert '不可見的B工作' not in md
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(call('alice', 'GET', '/api/exports/tasks.xlsx').data))
    assert book.active['B2'].data_type != 'f' and book.active['B2'].value == "'=1+2"


def test_one_collaboration_cannot_unlock_unrelated_detail_file_search_or_export(board):
    from test_evidence import upload
    _, ids, clients, call = board
    allowed = make_work(call, title='有權協作', collaborators=[ids['bob']])
    private = make_work(call, title='絕不可見的獨立資料')
    assert upload(clients, private, contents=b'private bytes').status_code == 200
    version = call('alice', 'GET', '/api/tasks/' + private['id'] + '/files').json['items'][0]
    assert call('bob', 'GET', '/api/tasks/' + allowed['id']).status_code == 200
    for endpoint in ['/api/tasks/' + private['id'], '/api/tasks/' + private['id'] + '/files',
                     '/api/files/' + version['id'] + '/download']:
        assert call('bob', 'GET', endpoint).status_code == 404
    for endpoint in ['/api/tasks?view=all', '/api/weekly', '/api/exports/tasks.csv', '/api/exports/wbs.md']:
        response = call('bob', 'GET', endpoint)
        assert response.status_code == 200
        assert private['id'] not in response.text and private['title'] not in response.text


def test_task_export_matches_owner_search_sort_and_visible_leaf_schedule(board):
    import csv
    _, _, _, call = board
    parent = make_work(call, title='B parent')
    leaf = make_work(call, title='A leaf', parent_id=parent['id'])
    assert call('alice', 'POST', '/api/tasks/' + leaf['id'] + '/plan', {'revision': leaf['revision'],
        'planned_start': '2026-12-01', 'planned_end': '2026-12-04', 'reason': '末層排程'}).status_code == 200
    hidden = make_work(call, 'bob', title='C private')
    response = call('alice', 'GET', '/api/exports/tasks.csv?q=alice&sort=title&direction=desc')
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
    assert [r[1] for r in rows[1:]] == ['B parent', 'A leaf']
    assert rows[1][6:8] == ['2026-12-01', '2026-12-04']
    assert hidden['title'] not in response.text


def test_download_denial_does_not_disclose_attachment_existence(board):
    from uuid import uuid4
    from test_evidence import upload
    _, _, clients, call = board
    task = make_work(call)
    assert upload(clients, task).status_code == 200
    version = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][0]
    forbidden = call('bob', 'GET', '/api/files/' + version['id'] + '/download')
    absent = call('bob', 'GET', '/api/files/' + str(uuid4()) + '/download')
    assert forbidden.status_code == absent.status_code == 404
    assert forbidden.json == absent.json
    assert call('alice', 'GET', '/api/files/' + version['id'] + '/download').status_code == 200


def test_unknown_workload_is_not_zero_and_proposal_is_not_committed(board):
    _, ids, _, call = board
    task = make_work(call)
    proposed = make_work(call, owner_id=ids['bob'], assignment='proposed')
    result = call('chief', 'GET', '/api/workload').json
    alice = next(p for p in result['items'] if p['person_id'] == ids['alice'])
    bob = next(p for p in result['items'] if p['person_id'] == ids['bob'])
    assert alice['unknown_tasks'] == 1 and alice['ratio'] is None
    assert bob['proposals'] == 1 and bob['visible_tasks'] == []
    assert call('alice', 'GET', '/api/workload').status_code == 403
    assert call('chief', 'PUT', '/api/capacity/' + ids['alice'], {'revision': 0, 'week': result['week'], 'hours': 30, 'reason': '本週可安排'}).status_code == 200
    assert call('alice', 'PUT', '/api/tasks/' + task['id'] + '/allocations', {'revision': 0, 'week': result['week'], 'hours': 5, 'remaining': 15}).status_code == 200
    alice = next(p for p in call('chief', 'GET', '/api/workload').json['items'] if p['person_id'] == ids['alice'])
    assert alice['ratio'] == 0.5 and alice['unknown_tasks'] == 0


def test_indirect_exports_preserve_their_view_permissions(board):
    from test_evidence import upload
    _, ids, clients, call = board
    task = make_work(call, collaborators=[ids['bob']])
    upload(clients, task)
    version = call('alice', 'GET', '/api/tasks/' + task['id'] + '/files').json['items'][0]
    assert call('alice', 'POST', '/api/service-terms', {'task_id': task['id'], 'basis_version_id': version['id'],
        'name': '不可外洩的合約', 'scope': '不可外洩的範圍', 'contact': '窗口', 'contract_reference': 'PRIVATE',
        'checked_status': '已核對', 'start_on': '2026-01-01', 'end_on': '2030-12-31', 'reason': '建立依據'}).status_code == 200
    assert call('alice', 'POST', '/api/coordinations', {'resource_type': 'task', 'resource_id': task['id'],
        'assignee_id': ids['bob'], 'subject': '不可外洩的協調', 'follow_on': '2030-01-01'}).status_code == 200
    for who in ['outsider', 'admin', 'cleaner']:
        for dataset in ['terms', 'coordinations']:
            response = call(who, 'GET', '/api/exports/' + dataset + '.csv')
            assert response.status_code == 200
            assert '不可外洩'.encode('utf-8') not in response.data and task['id'] not in response.text
    for who in ['alice', 'admin']:
        assert call(who, 'GET', '/api/exports/cleanup.csv').status_code == 403
    assert call('cleaner', 'GET', '/api/exports/templates.csv').status_code == 403


def test_anonymous_gets_never_take_global_mutation_lock(board):
    app, _, _, _ = board
    from sqlalchemy import event
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(app.extensions['engine'], 'before_cursor_execute', capture)
    try:
        client = app.test_client()
        assert client.get('/login').status_code == 200
        assert client.get('/api/auth/status').status_code == 200
        assert client.get('/does-not-exist').status_code == 404
        assert not any('update sequences' in stmt for stmt in statements)
    finally:
        event.remove(app.extensions['engine'], 'before_cursor_execute', capture)
def test_disabled_account_retains_visible_configured_roles_for_reenable(board):
    app, ids, _, call = board
    row = next(p for p in call('admin', 'GET', '/api/admin/people').json['items'] if p['id'] == ids['bob'])
    assert row['roles'] == ['member']
    assert call('admin', 'PATCH', '/api/admin/people/' + row['id'], {'revision': row['revision'], 'active': False, 'roles': row['roles'], 'reason': '停用驗證'}).status_code == 200
    row = next(p for p in call('admin', 'GET', '/api/admin/people').json['items'] if p['id'] == ids['bob'])
    assert row['roles'] == ['member'] and not row['active']
    assert call('bob', 'GET', '/api/tasks').status_code == 401
    assert call('admin', 'PATCH', '/api/admin/people/' + row['id'], {'revision': row['revision'], 'active': True, 'roles': row['roles'], 'reason': '恢復驗證'}).status_code == 200


def test_export_pages_enforce_their_own_permission_and_keep_private_tasks_hidden(board):
    from test_projects import project
    _, ids, _, call = board
    p = project(call, members=[ids['bob']])
    hidden = make_work(call, project_id=p['id'], title='不可洩漏的專案工作')
    assert call('alice', 'GET', '/api/exports/accounts.csv').status_code == 403
    assert call('alice', 'GET', '/api/exports/cleanup.csv').status_code == 403
    assert call('alice', 'GET', '/api/exports/load.xlsx').status_code == 403
    assert call('alice', 'GET', '/api/exports/department.md').status_code == 403
    result = call('bob', 'GET', '/api/exports/wbs.csv')
    assert result.status_code == 200 and p['code'].encode() in result.data
    assert hidden['title'].encode() not in result.data and hidden['code'].encode() not in result.data
    assert call('admin', 'GET', '/api/exports/accounts.xlsx').status_code == 200


def test_task_export_matches_cancelled_and_personal_view(board):
    _, _, _, call = board
    t = make_work(call, title='取消後仍可查詢')
    assert call('alice', 'POST', '/api/tasks/' + t['id'] + '/control', {'revision': t['revision'], 'action': 'cancel', 'reason': '不再需要'}).status_code == 200
    assert t['code'].encode() not in call('alice', 'GET', '/api/exports/tasks.csv?view=mine').data
    assert t['code'].encode() in call('alice', 'GET', '/api/exports/tasks.csv?view=mine&execution=cancelled').data
