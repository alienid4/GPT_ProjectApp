import json
from pathlib import Path

from conftest import make_work
from test_evidence import upload


def test_maintenance_observes_pending_queue_without_disclosing_business_data(board):
    app, _, clients, call = board
    task = make_work(call, title='維護者不得讀取的工作名稱')
    assert upload(clients, task, filename='restricted-business.txt').status_code == 200
    root = Path(app.config['STORAGE_PATH'])
    journal = root / '.pending-uploads'
    marker = journal / ('a' * 64)
    marker.write_text('opaque synthetic marker', encoding='utf-8')
    response = call('admin', 'GET', '/api/maintenance/status')
    assert response.status_code == 200
    data = response.json
    assert data['upload_checks'] == {'status':'observed','count':1,'limited':False}
    assert data['storage']['free_bytes'] > 0
    for secret in [task['id'],task['title'],'restricted-business.txt',str(root),'a' * 64]:
        assert secret not in json.dumps(data,ensure_ascii=False)
    for who in ['alice','chief','director','cleaner']:
        assert call(who, 'GET', '/api/maintenance/status').status_code == 403
        assert call(who, 'GET', '/api/maintenance/runbook.md').status_code == 403
    assert call('admin','GET','/api/tasks/'+task['id']).status_code == 404
    manual = call('admin', 'GET', '/api/maintenance/runbook.md')
    assert manual.status_code == 200 and '隔離測試環境操作維護' in manual.text
    assert manual.headers['Content-Disposition'].startswith('attachment;')


def test_maintenance_io_failure_is_unknown_not_zero(board, monkeypatch):
    import workboard.maintenance as maintenance
    _, _, _, call = board
    def denied(_):
        raise PermissionError('do not expose private storage path')
    monkeypatch.setattr(maintenance.shutil, 'disk_usage', denied)
    response = call('admin', 'GET', '/api/maintenance/status')
    assert response.status_code == 200
    assert response.json['upload_checks']['count'] is None
    assert response.json['storage']['status'] == 'unavailable'
    assert 'private storage path' not in response.text
