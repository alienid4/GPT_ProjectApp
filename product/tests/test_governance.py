import json
from conftest import make_work
from test_evidence import upload
from test_projects import project


def template(call):
    r = call('admin', 'POST', '/api/templates', {'name': '採購維運', 'phases': ['POC', '報價', '保養'], 'requires_confirmation': True})
    assert r.status_code == 200, r.json
    t = r.json['template']
    return call('admin', 'PATCH', '/api/templates/' + t['id'], {'revision': t['revision'], 'status': 'active', 'reason': '發布測試範本'}).json['template']


def test_template_generates_phases_and_keeps_project_snapshot_after_retirement(board):
    _, _, _, call = board
    t = template(call)
    p = project(call, template_id=t['id'], skipped_phases={'POC': '已有相同產品實測依據'})
    rows = call('alice', 'GET', '/api/tasks?project_id=' + p['id']).json['items']
    assert len(rows) == 2 and {r['phase'] for r in rows} == {'報價', '保養'}
    assert all(r['requires_confirmation'] for r in rows)
    assert p['template']['skipped_phases'] == {'POC': '已有相同產品實測依據'}
    assert call('admin', 'PATCH', '/api/templates/' + t['id'], {'revision': t['revision'], 'status': 'retired', 'reason': '換版'}).status_code == 200
    assert call('alice', 'GET', '/api/projects/' + p['id']).json['project']['template'] == p['template']
    assert call('alice', 'POST', '/api/projects', {'name': '不可採用舊範本', 'goal': '測試', 'template_id': t['id']}).status_code == 400


def test_skipping_a_phase_requires_reason_and_cannot_skip_every_phase(board):
    _, _, _, call = board
    t = template(call)
    for skipped in [{'POC': ''}, {'未存在階段': '略過'}, {p: '略過' for p in t['phases']}]:
        r = call('alice', 'POST', '/api/projects', {'name': '無效專案', 'goal': '測試', 'template_id': t['id'], 'skipped_phases': skipped})
        assert r.status_code == 400
    assert call('alice', 'GET', '/api/projects').json['items'] == []


def test_shared_planning_does_not_allow_changing_members_or_reading_private_tasks(board):
    _, ids, _, call = board
    p = project(call, members=[ids['bob']])
    t = make_work(call, project_id=p['id'])
    assert call('bob', 'PATCH', '/api/projects/' + p['id'], {'revision': p['revision'], 'name': '共同調整名稱', 'reason': '規劃修改'}).status_code == 200
    p = call('alice', 'GET', '/api/projects/' + p['id']).json['project']
    path = '/api/projects/' + p['id'] + '/members'
    assert call('bob', 'PATCH', path, {'revision': p['revision'], 'members': [ids['outsider']], 'reason': '擴權'}).status_code == 403
    assert call('bob', 'GET', '/api/tasks/' + t['id']).status_code == 404
    assert call('alice', 'PATCH', path, {'revision': p['revision'], 'members': [], 'reason': '撤銷規劃'}).status_code == 200
    assert call('bob', 'GET', '/api/projects/' + p['id']).status_code == 404


def test_coordination_has_explicit_handoff_and_never_completes_the_work(board):
    _, ids, _, call = board
    t = make_work(call, collaborators=[ids['bob']])
    c = call('alice', 'POST', '/api/coordinations', {'resource_type': 'task', 'resource_id': t['id'], 'assignee_id': ids['bob'], 'subject': '協調停機窗口', 'follow_on': '2026-12-01'}).json['coordination']
    path = '/api/coordinations/' + c['id'] + '/actions'
    assert call('alice', 'POST', path, {'revision': c['revision'], 'action': 'accept', 'reason': '代受理'}).status_code == 403
    accepted = call('bob', 'POST', path, {'revision': c['revision'], 'action': 'accept', 'reason': '受理核對'}).json['coordination']
    assert call('bob', 'POST', path, {'revision': accepted['revision'], 'action': 'complete', 'reason': '窗口已協調'}).json['coordination']['status'] == 'completed'
    assert call('alice', 'GET', '/api/tasks/' + t['id']).json['task']['status'] == 'todo'
    assert call('outsider', 'GET', '/api/coordinations').json['items'] == []


def test_service_terms_are_bound_to_current_access_and_immutable_basis(board):
    _, ids, clients, call = board
    t = make_work(call, collaborators=[ids['bob']])
    upload(clients, t)
    v = call('alice', 'GET', '/api/tasks/' + t['id'] + '/files').json['items'][0]
    r = call('alice', 'POST', '/api/service-terms', {'task_id': t['id'], 'basis_version_id': v['id'], 'name': '備份設備保養', 'scope': '設備維修', 'contact': '合約窗口', 'contract_reference': 'TEST-CONTRACT', 'checked_status': '已核對', 'start_on': '2026-11-01', 'end_on': '2027-10-31', 'reason': '建立服務條件'})
    assert r.status_code == 200, r.json
    term = r.json['term']
    assert call('bob', 'POST', '/api/service-terms/' + term['id'] + '/logs', {'happened_on': '2026-11-22', 'note': '完成檢查'}).status_code == 200
    assert call('bob', 'PATCH', '/api/service-terms/' + term['id'], {'revision': term['revision'], 'contact': '擅改', 'reason': '測試'}).status_code == 403
    call('chief', 'POST', '/api/tasks/' + t['id'] + '/arrangement', {'revision': t['revision'], 'action': 'remove_collaborator', 'person_id': ids['bob'], 'reason': '撤權'})
    assert call('bob', 'GET', '/api/service-terms').json['items'] == []
    assert call('bob', 'GET', '/api/service-terms/' + term['id'] + '/logs').status_code == 404
def test_delegated_grant_cannot_self_promote_or_delegate_grant_again(board):
    _, ids, _, call = board
    p = project(call, members=[ids['bob'], ids['chief']])
    path = '/api/grants/project/' + p['id']
    assert call('chief', 'POST', path, {'person_id': ids['bob'], 'capability': 'grant', 'reason': '授予範圍管理'}).status_code == 200
    assert call('bob', 'POST', path, {'person_id': ids['bob'], 'capability': 'confirm', 'reason': '自授確認'}).status_code == 403
    assert call('bob', 'POST', path, {'person_id': ids['alice'], 'capability': 'grant', 'reason': '再次轉授'}).status_code == 403


def test_active_contract_cleanup_requires_ack_without_extending_its_expiry(board):
    from test_retention_backup import expire
    from workboard.models import ServiceTerm
    app, _, clients, call = board
    t = make_work(call)
    upload(clients, t)
    v = call('alice', 'GET', '/api/tasks/' + t['id'] + '/files').json['items'][0]
    term = call('alice', 'POST', '/api/service-terms', {'task_id': t['id'], 'basis_version_id': v['id'], 'name': '長約紀錄', 'scope': '維修', 'contact': '虛構窗口', 'contract_reference': 'TEST', 'checked_status': '已核對', 'start_on': '2022-01-01', 'end_on': '2031-12-31', 'reason': '驗證保存'}).json['term']
    expire(app, ServiceTerm, term['id'])
    item = next(r for r in call('cleaner', 'GET', '/api/cleanup').json['items'] if r['resource_id'] == term['id'])
    assert item['impacts'] == ['active_service'] and '長約紀錄' not in str(item)
    selection = {'resource_type': 'service_term', 'resource_id': term['id'], 'revision': item['revision']}
    body = {'action': 'clean', 'confirmed': True, 'reason': '已核對有效合約影響', 'items': [selection]}
    assert call('cleaner', 'POST', '/api/cleanup', body).json['results'][0]['status'] == 'failed'
    selection['acknowledge_impacts'] = True
    assert call('cleaner', 'POST', '/api/cleanup', body).json['results'][0]['status'] == 'done'
    with app.extensions['sessions']() as db:
        assert db.get(ServiceTerm, term['id']).expires_at.isoformat() + 'Z' == item['expires_at']


def test_time_limiting_last_confidential_steward_requires_successor(board):
    from datetime import timedelta
    from workboard.models import utcnow
    _, ids, _, call = board
    task = make_work(call, collaborators=[ids['bob']])
    path = '/api/grants/task/' + task['id']
    for who, capability in [('alice', 'confidential'), ('alice', 'grant'), ('bob', 'grant')]:
        assert call('chief', 'POST', path, {'person_id': ids[who], 'capability': capability, 'reason': '安排管理'}).status_code == 200
    assert call('chief', 'PUT', '/api/tasks/' + task['id'] + '/access', {'revision': task['access_revision'],
        'assignment_revision': task['revision'], 'kind': 'confidential', 'confidential': True,
        'readers': [ids['alice'], ids['bob']], 'reason': '指定機密讀者', 'acknowledge_excluded': True}).status_code == 200
    change = {'person_id': ids['alice'], 'capability': 'confidential',
              'valid_until': (utcnow() + timedelta(hours=8)).date().isoformat(), 'reason': '縮短期限'}
    assert call('bob', 'POST', path, change).status_code == 409
    assert call('alice', 'POST', path, {'person_id': ids['bob'], 'capability': 'confidential', 'reason': '接任管理'}).status_code == 200
    assert call('alice', 'POST', path, {'person_id': ids['bob'], 'capability': 'confidential',
        'valid_until': (utcnow() + timedelta(days=2, hours=8)).date().isoformat(), 'reason': '有限期接任'}).status_code == 200
    assert call('bob', 'POST', path, change).status_code == 409
    assert call('alice', 'POST', path, {'person_id': ids['bob'], 'capability': 'confidential', 'reason': '持續管理'}).status_code == 200
    assert call('bob', 'POST', path, change).status_code == 200
