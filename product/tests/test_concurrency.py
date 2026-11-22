from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4
from conftest import make_work


def concurrent_requests(app, clients, requests):
    cookie = clients['alice'][0].get_cookie('workboard_session').value
    csrf = clients['alice'][1]
    barrier = Barrier(len(requests))
    def run(spec):
        method, path, data, key = spec
        client = app.test_client()
        client.set_cookie('workboard_session', cookie)
        barrier.wait(timeout=5)
        return client.open(path, method=method, json=data,
                           headers={'X-CSRF-Token': csrf, 'Idempotency-Key': key})
    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(run, requests))


def test_parallel_creates_have_distinct_codes_and_same_key_replays(board):
    app, _, clients, call = board
    specs = [('POST', '/api/tasks', {'title': '並行工作 ' + str(i), 'commitment': '驗證編號'}, str(uuid4())) for i in range(4)]
    responses = concurrent_requests(app, clients, specs)
    assert [r.status_code for r in responses] == [200] * 4
    assert len({r.json['task']['code'] for r in responses}) == 4
    key = str(uuid4())
    same = ('POST', '/api/tasks', {'title': '同一要求只建立一次', 'commitment': '驗證重試'}, key)
    responses = concurrent_requests(app, clients, [same, same])
    assert [r.status_code for r in responses] == [200, 200]
    assert len({r.json['task']['id'] for r in responses}) == 1
    assert sorted(r.json['replayed'] for r in responses) == [False, True]


def test_parallel_edits_never_silently_overwrite_same_revision(board):
    app, _, clients, call = board
    task = make_work(call)
    responses = concurrent_requests(app, clients, [('PATCH', '/api/tasks/' + task['id'],
        {'revision': task['revision'], 'description': description, 'reason': '同版並行'}, str(uuid4())) for description in ['A的變更', 'B的變更']])
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(r for r in responses if r.status_code == 200).json['task']
    actual = call('alice', 'GET', '/api/tasks/' + task['id']).json['task']
    assert actual['description'] == winner['description'] and actual['revision'] == task['revision'] + 1


def test_slow_identity_provider_does_not_hold_global_write_lock(board, monkeypatch):
    app, _, clients, call = board
    entered, release = Event(), Event()
    provider = app.extensions['identity_provider']
    original = provider.revalidate
    def slow(db, person):
        if person.username == 'bob':
            entered.set()
            assert release.wait(timeout=5)
        return original(db, person)
    monkeypatch.setattr(provider, 'revalidate', slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call, 'bob', 'POST', '/api/tasks', {'title': '延遲目錄查詢', 'commitment': '隔離延遲'})
        assert entered.wait(timeout=2)
        try:
            response = call('alice', 'POST', '/api/tasks', {'title': '不受其他目錄等待影響', 'commitment': '可正常寫入'})
            assert response.status_code == 200
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 200
    # This exercises ordering with a controlled provider delay, not real AD.
