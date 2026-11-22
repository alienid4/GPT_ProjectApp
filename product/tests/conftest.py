from uuid import uuid4
import pytest
from sqlalchemy import select
from workboard.app import create_app
from workboard.models import Base, Person


@pytest.fixture
def board(tmp_path):
    live = tmp_path / 'live'
    live.mkdir()
    app = create_app({"TESTING": True, "APP_MODE": "test", "DATABASE_URL": "sqlite:///" + (live / 'database.sqlite').as_posix(), "STORAGE_PATH": str(live / "files")})
    Base.metadata.create_all(app.extensions["engine"])
    ids = {}
    with app.extensions["sessions"].begin() as db:
        for username, role in [("alice", "member"), ("bob", "member"), ("outsider", "member"), ("chief", "chief"), ("director", "director"), ("admin", "maintenance"), ('cleaner', 'cleaner')]:
            p = Person(directory_id="test:" + username, username=username, name=username, department="D", role=role)
            db.add(p)
            db.flush()
            ids[username] = p.id
    clients = {}
    for username in ids:
        client = app.test_client()
        csrf = client.get('/api/auth/status').json['csrf']
        r = client.post('/api/auth/login', json={'username': username}, headers={'X-CSRF-Token': csrf})
        assert r.status_code == 200
        clients[username] = (client, r.json['csrf'])
    def call(username, method, path, data=None, key=None, **kwargs):
        client, csrf = clients[username]
        headers = {'X-CSRF-Token': csrf, 'Idempotency-Key': key or str(uuid4())}
        return client.open(path, method=method, json=data, headers=headers, **kwargs)
    yield app, ids, clients, call
    app.extensions['engine'].dispose()


def make_work(call, who='alice', **data):
    response = call(who, 'POST', '/api/tasks', {'title': '機房檢查', 'commitment': '完成巡檢與異常記錄', **data})
    assert response.status_code == 200, response.json
    return response.json['task']
