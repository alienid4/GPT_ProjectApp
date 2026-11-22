import csv
import io
from datetime import timedelta

from conftest import make_work
from test_evidence import upload
from test_projects import project
from workboard.models import Task, retention_end, utcnow
from workboard.sorting import ordered


def csv_rows(response):
    assert response.status_code == 200, response.json
    return list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))


def test_files_export_matches_work_title_search_and_never_includes_restricted_files(board):
    _, ids, clients, call = board
    task = make_work(call, title='機房搬遷 10', collaborators=[ids['bob']])
    assert upload(clients, task, filename='normal.txt').status_code == 200
    assert upload(clients, task, filename='hidden.txt', readers=[ids['alice']]).status_code == 200
    rows = csv_rows(call('bob', 'GET', '/api/exports/files.csv?q=機房搬遷&sort=filename'))
    assert [r[1] for r in rows[1:]] == ['normal.txt']
    assert rows[1][5] == task['title']
    assert len(csv_rows(call('outsider', 'GET', '/api/exports/files.csv?q=機房搬遷'))) == 1


def test_wbs_export_sorts_siblings_and_keeps_filtered_depth_consistent(board):
    _, _, _, call = board
    root = project(call, name='搬遷總案')
    sub10 = project(call, name='區域 10', parent_id=root['id'])
    sub2 = project(call, name='區域 2', parent_id=root['id'])
    child = make_work(call, project_id=sub2['id'], title='子項 3')
    parent_task = make_work(call, project_id=root['id'], title='現場工作')
    rows = csv_rows(call('alice', 'GET', '/api/exports/wbs.csv?sort=title'))
    assert [r[3] for r in rows[1:]] == ['搬遷總案','區域 2','子項 3','區域 10','現場工作']
    filtered = csv_rows(call('alice', 'GET', '/api/exports/wbs.csv?sort=title&q=子項'))
    assert len(filtered) == 2 and filtered[1][0] == '2' and filtered[1][5] == sub2['code']
    assert root['id'] not in str(call('outsider', 'GET', '/api/exports/wbs.md').data)


def test_export_status_order_uses_same_raw_values_as_page(board):
    _, _, _, call = board
    waiting = project(call, name='等待案')
    doing = project(call, name='執行案')
    for row, status in [(waiting,'waiting'), (doing,'doing')]:
        result = call('alice', 'POST', '/api/projects/' + row['id'] + '/actions', {
            'revision': row['revision'], 'action': 'status', 'status': status,
            'next_action': '追蹤進度', 'follow_on': '2026-12-01', 'wait_for': '供應商', 'reason':'測試狀態排序'})
        assert result.status_code == 200, result.json
    rows = csv_rows(call('alice', 'GET', '/api/exports/projects.csv?sort=status'))
    assert [r[1] for r in rows[1:]] == ['執行案','等待案']


def test_cleanup_export_accepts_same_chinese_search_as_page(board):
    app, _, _, call = board
    task = make_work(call, title='到期合成工作')
    with app.extensions['sessions'].begin() as db:
        record = db.get(Task, task['id'])
        record.created_at = utcnow() - timedelta(days=1200)
        record.expires_at = retention_end(record.created_at)
    rows = csv_rows(call('cleaner', 'GET', '/api/exports/cleanup.csv?q=工作&sort=expires_at'))
    assert any(r[0] == '工作' and r[1] == task['id'] for r in rows[1:])
    assert call('alice', 'GET', '/api/exports/cleanup.csv?q=工作').status_code == 403


def test_natural_unicode_order_and_unknown_values_are_stable_in_both_directions():
    records = [{'id':'c','title':'機房 10'}, {'id':'b','title':'機房 2'}, {'id':'a','title':'機房 02'},
               {'id':'d','title':None}, {'id':'e','title':''}]
    assert [r['id'] for r in ordered(records,'title')] == ['a','b','c','d','e']
    assert [r['id'] for r in ordered(records,'title',True)] == ['c','b','a','e','d']
