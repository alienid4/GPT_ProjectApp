"""Editor metadata must expose only current, authorized choices."""
from datetime import timedelta

from conftest import make_work
from test_evidence import upload
from test_projects import project
from workboard.models import CapabilityGrant, Person, utcnow


def test_file_editor_candidates_follow_current_task_and_version_access(board):
    app, ids, clients, call = board
    task = make_work(call, collaborators=[ids['bob']])
    path = '/api/tasks/' + task['id'] + '/files?editor=1'
    assert upload(clients, task, readers=[ids['alice'], ids['bob']]).status_code == 200
    data = call('alice', 'GET', path).json
    assert set(data['reader_candidates']) == {ids[n] for n in ['alice', 'bob', 'chief', 'director']}
    assert set(data['items'][0]['readers']) == {ids['alice'], ids['bob']}
    assert call('outsider', 'GET', path).status_code == 404
    # A department reader is not an editor and cannot inspect editor lists.
    data = call('chief', 'GET', path).json
    assert data['reader_candidates'] == [] and data['items'] == []
    with app.extensions['sessions'].begin() as db:
        db.get(Person, ids['bob']).directory_active = False
    data = call('alice', 'GET', path).json
    assert ids['bob'] not in data['reader_candidates']
    # Historical ACL stays intact; only currently eligible candidates can be selected.
    assert ids['bob'] in data['items'][0]['readers']


def test_readonly_file_reader_gets_no_acl_edit_metadata(board):
    _, _, clients, call = board
    task = make_work(call)
    assert upload(clients, task).status_code == 200
    data = call('chief', 'GET', '/api/tasks/' + task['id'] + '/files?editor=1').json
    assert len(data['items']) == 1
    assert 'readers' not in data['items'][0]
    assert data['reader_candidates'] == []


def test_later_collaborator_cannot_see_or_extend_private_document_but_can_add_own(board):
    _, ids, clients, call = board
    task = make_work(call, collaborators=[ids['bob']])
    path = '/api/tasks/' + task['id'] + '/files?editor=1'
    assert upload(clients, task, readers=[ids['alice']]).status_code == 200
    private = call('alice', 'GET', path).json['items'][0]
    assert call('bob', 'GET', path).json['items'] == []
    assert upload(clients, task, who='bob', document_id=private['document_id']).status_code == 404
    assert upload(clients, task, who='bob', readers=[ids['bob']]).status_code == 200
    own = call('bob', 'GET', path).json['items']
    assert len(own) == 1 and own[0]['document_id'] != private['document_id']
    assert own[0]['readers'] == [ids['bob']]


def test_project_dispatch_hint_tracks_grant_and_expiry_without_cross_project_leak(board):
    app, ids, _, call = board
    first = project(call, members=[ids['bob']])
    second = project(call, members=[ids['bob']])
    path = '/api/projects/' + first['id']
    assert not call('bob', 'GET', path).json['project']['permissions']['dispatch']
    with app.extensions['sessions'].begin() as db:
        grant = CapabilityGrant(person_id=ids['bob'], scope_type='project', scope_id=first['id'],
                                capability='dispatch', granted_by=ids['chief'])
        db.add(grant)
    assert call('bob', 'GET', path).json['project']['permissions']['dispatch']
    assert not call('bob', 'GET', '/api/projects/' + second['id']).json['project']['permissions']['dispatch']
    assigned = call('bob', 'POST', '/api/tasks', {'title': '委派派工', 'project_id': first['id'],
                    'owner_id': ids['alice'], 'assignment': 'assigned'})
    assert assigned.status_code == 200 and assigned.json == {'ok': True, 'access_ended': True}
    assert any(t['title'] == '委派派工' and t['assignment'] == 'assigned'
               for t in call('alice', 'GET', '/api/tasks').json['items'])
    with app.extensions['sessions'].begin() as db:
        db.query(CapabilityGrant).filter_by(person_id=ids['bob']).update({'valid_until': utcnow() - timedelta(seconds=1)})
    assert not call('bob', 'GET', path).json['project']['permissions']['dispatch']
    assert call('bob', 'POST', '/api/tasks', {'title': 'expired dispatch', 'project_id': first['id'],
                'owner_id': ids['alice'], 'assignment': 'assigned'}).status_code == 403
