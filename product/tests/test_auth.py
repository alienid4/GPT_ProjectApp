import pytest
from sqlalchemy import select

from workboard.app import create_app
from workboard.auth import TestIdentityProvider as IdentityForTests
from workboard.models import Base, LoginSession, Person


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "APP_MODE": "test", "DATABASE_URL": "sqlite://"})
    Base.metadata.create_all(app.extensions["engine"])
    with app.extensions["sessions"].begin() as db:
        db.add(Person(directory_id="test:alice", username="alice", name="測試同仁", department="D"))
    yield app
    app.extensions["engine"].dispose()


def login(client):
    state = client.get('/api/auth/status').json
    response = client.post('/api/auth/login', json={"username": "alice"}, headers={"X-CSRF-Token": state["csrf"]})
    assert response.status_code == 200
    return response.json


def test_login_page_is_a_real_login_not_an_in_page_role_switch(app):
    client = app.test_client()
    assert client.get('/').status_code == 302
    page = client.get('/login')
    assert page.status_code == 200 and '隔離測試登入' in page.text
    assert client.get('/api/me').status_code == 401
    result = login(client)
    assert result['user']['name'] == '測試同仁'
    assert client.get('/api/me').status_code == 200


def test_missing_csrf_and_forged_principal_are_rejected(app):
    client = app.test_client()
    assert client.post('/api/auth/login', json={'username': 'alice'}).status_code == 403
    token = client.get('/api/auth/status').json['csrf']
    result = client.post('/api/auth/login', json={'username': 'unknown', 'role': 'chief'}, headers={'X-CSRF-Token': token})
    assert result.status_code == 401


def test_session_revocation_and_disabled_user_apply_on_the_next_request(app):
    client = app.test_client()
    login(client)
    with app.extensions['sessions'].begin() as db:
        actor = db.scalar(select(Person).where(Person.username == 'alice'))
        actor.active = False
        actor.auth_epoch += 1
    assert client.get('/api/me').status_code == 401


def test_multiple_tabs_do_not_rotate_each_others_csrf(app):
    client = app.test_client()
    result = login(client)
    assert client.get('/api/auth/status').json['csrf'] == result['csrf']
    assert client.get('/api/auth/status').json['csrf'] == result['csrf']
    assert client.post('/api/auth/logout', headers={'X-CSRF-Token': result['csrf']}).status_code == 200
    assert client.get('/api/me').status_code == 401


def test_session_cookie_is_http_only_and_database_has_only_its_hash(app):
    client = app.test_client()
    login(client)
    cookie = client.get_cookie('workboard_session')
    assert cookie.http_only and cookie.same_site == 'Strict'
    with app.extensions['sessions']() as db:
        row = db.scalar(select(LoginSession))
        assert row.token_hash != cookie.value and len(row.token_hash) == 64


def test_test_provider_is_never_eligible_in_production():
    with pytest.raises(RuntimeError):
        IdentityForTests('production')
    with pytest.raises(RuntimeError, match='SQL Server'):
        create_app({'APP_MODE': 'production', 'DATABASE_URL': 'sqlite://'})


def test_security_headers_and_no_cache(app):
    response = app.test_client().get('/login')
    assert response.headers['Cache-Control'] == 'no-store'
    assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
