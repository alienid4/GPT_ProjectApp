"""Isolated SQLite rehearsal with matched immutable blobs and current ACLs.

This adapter refuses production databases. SQL Server backup/restore requires
the company's native backup adapter and is never reported passed by this tool.
"""
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.orm import sessionmaker

from .database import database
from .errors import DomainError, require
from .models import (AccessReader, Base, CapabilityGrant, CleanupHistory, CleanupItem, Comment, Coordination,
                     DocumentVersion, LoginSession, Person, Project, ProjectMember,
                     RequestKey, Sequence, Task, TaskAccess, TaskRelation, VersionReader, utcnow)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def private_path(root, key):
    import re
    require(isinstance(key, str) and re.fullmatch('[0-9a-f]{64}', key), '附件索引不符。')
    root = Path(root).resolve()
    path = root / key[:2] / key
    require(root in path.resolve().parents and not path.is_symlink(), '附件路徑不符。')
    return path


def lock(db):
    count = db.execute(update(Sequence).where(Sequence.key == 'application_transaction').values(value=Sequence.value)).rowcount
    if not count:
        db.add(Sequence(key='application_transaction', value=1))
        db.flush()


def sqlite_file(engine):
    require(engine.dialect.name == 'sqlite' and engine.url.database not in {None, '', ':memory:'},
            '此工具僅供獨立 SQLite 測試演練；正式 SQL Server 尚未驗證。')
    return Path(engine.url.database).resolve()


def create_backup(engine, storage_root, destination):
    source = sqlite_file(engine)
    destination = Path(destination).resolve()
    require(not destination.exists() and destination != source.parent and source.parent not in destination.parents,
            '備份目的地須為來源資料目錄以外的新目錄。')
    destination.mkdir(parents=True, mode=0o700)
    sessions = sessionmaker(engine, expire_on_commit=False)
    manifest = {'format': 1, 'adapter': 'isolated-sqlite-rehearsal', 'created_at': utcnow().isoformat() + 'Z',
                'database': 'database.sqlite', 'files': [], 'formal_integration': 'NOT VERIFIED'}
    try:
        with sessions.begin() as db:
            lock(db)
            # Separate read connection avoids backing up an open write txn.
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as live, closing(sqlite3.connect(destination / 'database.sqlite')) as target:
                live.backup(target)
                # Every backup copy is born blocked and has no reusable login.
                # This changes ONLY the backup copy, never the live source.
                target.execute("INSERT OR REPLACE INTO sequences (key,value) VALUES ('restore_ready',0)")
                target.execute('DELETE FROM login_sessions')
                target.execute('DELETE FROM request_keys')
                target.commit()
            for version in db.scalars(select(DocumentVersion).where(DocumentVersion.deleted.is_(False))):
                blob = private_path(storage_root, version.blob_key)
                require(blob.is_file() and sha(blob) == version.sha256, '備份遇到缺檔或雜湊不一致，不能標示成功。', 409)
                target = private_path(destination / 'attachments', version.blob_key)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(blob, target)
                require(sha(target) == version.sha256, '備份附件核對失敗。', 409)
                manifest['files'].append({'version_id': version.id, 'key': version.blob_key, 'sha256': version.sha256, 'size': version.size})
            manifest['database_sha256'] = sha(destination / 'database.sqlite')
            # Application sources are paired without copying environment secrets.
            code_root = Path(__file__).resolve().parent
            manifest['code'] = {}
            for path in code_root.rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix in {'.py', '.html', '.css', '.js'}:
                    relative = path.relative_to(code_root)
                    target = destination / 'code' / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
                    manifest['code'][relative.as_posix()] = sha(path)
            (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        try:
            (destination / 'INCOMPLETE').write_text('Backup did not pass verification. Do not restore as a complete recovery point.', encoding='utf-8')
        except OSError:
            pass
        raise
    return manifest


def access_ledger(db):
    """Read current authorization while the caller holds the write barrier."""
    models = [Person, TaskAccess, AccessReader, TaskRelation, CapabilityGrant, ProjectMember, VersionReader, CleanupItem, CleanupHistory]
    ledger = {'observed_at': utcnow().isoformat() + 'Z'}
    for model in models:
        ledger[model.__tablename__] = [{column.name: getattr(row, column.name) for column in model.__table__.columns}
                                      for row in db.scalars(select(model))]
    # Current assignment relations must replace the old backup's owners.
    ledger['tasks'] = [{key: getattr(task, key) for key in ['id', 'owner_id', 'proposed_owner_id', 'creator_id', 'assignment', 'acknowledged', 'department', 'deleted']}
                       for task in db.scalars(select(Task))]
    ledger['projects'] = [{key: getattr(project, key) for key in ['id', 'owner_id', 'department']}
                          for project in db.scalars(select(Project))]
    ledger['versions'] = [{'id': v.id, 'restricted': v.restricted, 'deleted': v.deleted, 'scan_status': v.scan_status}
                          for v in db.scalars(select(DocumentVersion))]
    ledger['comments'] = [{'id': row.id, 'withdrawn': row.withdrawn} for row in db.scalars(select(Comment))]
    return ledger


def export_current_access(engine):
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        lock(db)
        return access_ledger(db)


def restore_backup(backup_dir, target_dir, current_engine):
    # Keep source mutations blocked until all destination checks finish. A
    # source revocation cannot commit between the ACL read and ready marker.
    sqlite_file(current_engine)
    sessions = sessionmaker(current_engine, expire_on_commit=False)
    with sessions.begin() as db:
        lock(db)
        return _restore_backup(backup_dir, target_dir, current_engine, access_ledger(db))


def _restore_backup(backup_dir, target_dir, current_engine, ledger):
    backup_dir, target_dir = Path(backup_dir).resolve(), Path(target_dir).resolve()
    require(not target_dir.exists() and target_dir != backup_dir and backup_dir not in target_dir.parents,
            '還原目的地須為獨立的新目錄。')
    current_file = sqlite_file(current_engine)
    require(target_dir != current_file.parent and current_file.parent not in target_dir.parents, '不可覆寫目前資料目錄。')
    require(not (backup_dir / 'INCOMPLETE').exists(), '備份未通過完整性檢查。', 409)
    manifest = json.loads((backup_dir / 'manifest.json').read_text(encoding='utf-8'))
    require(manifest.get('format') == 1 and manifest.get('adapter') == 'isolated-sqlite-rehearsal', '備份格式不符。')
    require(sha(backup_dir / 'database.sqlite') == manifest.get('database_sha256'), '資料庫備份雜湊不符。', 409)
    # Query current revocations before writing any ready recovery state. Failure
    # is blocking; there is no fallback to backed-up authorizations.
    target_dir.mkdir(parents=True, mode=0o700)
    (target_dir / 'RESTORE_BLOCKED').write_text('Verification in progress; do not serve.', encoding='utf-8')
    shutil.copyfile(backup_dir / 'database.sqlite', target_dir / 'database.sqlite')
    engine, sessions = database('sqlite:///' + (target_dir / 'database.sqlite').as_posix(), 'test')
    try:
        with sessions.begin() as db:
            ready = db.get(Sequence, 'restore_ready')
            if ready:
                ready.value = 0
            else:
                db.add(Sequence(key='restore_ready', value=0))
            db.execute(delete(LoginSession))
            db.execute(delete(RequestKey))
        with sessions.begin() as db:
            people = {p.id: p for p in db.scalars(select(Person))}
            current_people = {p['id']: p for p in ledger['people']}
            for pid, person in people.items():
                person.active = False
                person.auth_epoch += 1
                if pid in current_people:
                    current = current_people[pid]
                    for key in ['directory_id', 'username', 'name', 'department', 'role', 'roles_json', 'active', 'directory_active']:
                        setattr(person, key, current[key])
                    person.auth_epoch = max(person.auth_epoch, current['auth_epoch'] + 1)
            for pid, values in current_people.items():
                if pid not in people:
                    values = dict(values)
                    values['auth_epoch'] += 1
                    person = Person(**values)
                    db.add(person)
                    people[pid] = person
            db.flush()
            tasks = {t.id: t for t in db.scalars(select(Task))}
            projects = {p.id: p for p in db.scalars(select(Project))}
            current_tasks = {t['id']: t for t in ledger['tasks']}
            for tid, task in tasks.items():
                current = current_tasks.get(tid)
                if current:
                    for key in ['owner_id', 'proposed_owner_id', 'creator_id', 'assignment', 'acknowledged', 'department', 'deleted']:
                        setattr(task, key, current[key])
                else:
                    task.deleted = True
            for current in ledger['projects']:
                if current['id'] in projects:
                    projects[current['id']].owner_id = current['owner_id']
                    projects[current['id']].department = current['department']
            # Revoked relations are removed, not merged back from backup.
            for model in [AccessReader, TaskRelation, CapabilityGrant, ProjectMember, VersionReader]:
                db.execute(delete(model))
            db.execute(delete(TaskAccess))
            for values in ledger['task_access']:
                if values['task_id'] in tasks:
                    db.add(TaskAccess(**values))
            for tid in set(tasks) - {a['task_id'] for a in ledger['task_access']}:
                db.add(TaskAccess(task_id=tid, confidential=True))  # no readers
            db.flush()
            for model in [AccessReader, TaskRelation]:
                for values in ledger[model.__tablename__]:
                    if values['task_id'] in tasks and values['person_id'] in people:
                        db.add(model(**values))
            for values in ledger['capability_grants']:
                exists = values['scope_id'] in (tasks if values['scope_type'] == 'task' else projects)
                if exists and values['person_id'] in people and values['granted_by'] in people:
                    db.add(CapabilityGrant(**values))
            for values in ledger['project_members']:
                if values['project_id'] in projects and values['person_id'] in people:
                    db.add(ProjectMember(**values))
            versions = {v.id: v for v in db.scalars(select(DocumentVersion))}
            current_versions = {v['id']: v for v in ledger['versions']}
            for vid, version in versions.items():
                current = current_versions.get(vid)
                if current:
                    version.restricted, version.deleted, version.scan_status = current['restricted'], current['deleted'], current['scan_status']
                else:
                    version.deleted = True
            for values in ledger['version_readers']:
                if values['version_id'] in versions and values['person_id'] in people:
                    db.add(VersionReader(**values))
            current_comments = {row['id']: row for row in ledger['comments']}
            for comment in db.scalars(select(Comment)):
                current = current_comments.get(comment.id)
                if current is None or current['withdrawn']:
                    comment.body, comment.withdrawn = '', True
            # Preserve the current cleanup ledger and its original attribution.
            # A recovered database must not list already-cleaned records again.
            db.execute(delete(CleanupItem))
            db.execute(delete(CleanupHistory))
            for values in ledger['cleanup_items']:
                db.add(CleanupItem(**dict(values)))
            for values in ledger['cleanup_history']:
                db.add(CleanupHistory(**dict(values)))
            db.flush()
            from .retention import TARGETS, clear_content
            for item in ledger['cleanup_items']:
                model = TARGETS.get(item['resource_type'])
                if model is not None and item['status'] in {'done', 'pending_file', 'failed_file'}:
                    record = db.get(model, item['resource_id'])
                    if record is not None:
                        clear_content(record)
                        recovered_item = db.get(CleanupItem, item['id'])
                        if item['resource_type'] == 'version' and record.deleted:
                            recovered_item.status, recovered_item.failure_code = 'done', ''
                        db.add(CleanupHistory(resource_type=item['resource_type'], resource_id=item['resource_id'],
                                              actor_id=None, action='restore_apply', reason='還原套用現行清理紀錄',
                                              status=recovered_item.status))
            # Coordination rows are historical; they cannot create read access.
            for coordination in db.scalars(select(Coordination)):
                if coordination.status in {'pending', 'accepted'}:
                    coordination.status = 'restore_review'
            expected_files = {v.id: v for v in db.scalars(select(DocumentVersion).where(DocumentVersion.deleted.is_(False)))}
            require(set(expected_files) <= {f['version_id'] for f in manifest['files']}, '備份索引與附件清單不一致。', 409)
            for item in manifest['files']:
                version = versions.get(item['version_id'])
                require(version is not None, '備份附件索引不符。', 409)
                require(item['key'] == version.blob_key and item['sha256'] == version.sha256 and item['size'] == version.size, '附件清單不一致。', 409)
                if version.id not in expected_files:
                    continue  # a later cleanup/revocation must not be revived
                source = private_path(backup_dir / 'attachments', item['key'])
                require(source.is_file() and sha(source) == version.sha256, '還原缺檔或雜湊不符。', 409)
                destination = private_path(target_dir / 'attachments', item['key'])
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, destination)
                require(sha(destination) == version.sha256, '還原附件核對失敗。', 409)
            code_root = (backup_dir / 'code').resolve()
            for name, expected_sha in manifest.get('code', {}).items():
                relative = Path(name)
                source = (code_root / relative).resolve()
                require(not relative.is_absolute() and '..' not in relative.parts and code_root in source.parents,
                        '程式備份路徑不符。', 409)
                require(source.is_file() and sha(source) == expected_sha, '程式備份雜湊不符。', 409)
                destination = target_dir / 'code' / 'workboard' / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            db.get(Sequence, 'restore_ready').value = 1
        # Retain a content-free report; no role list or password is exported.
        report = {'status': 'PASS', 'adapter': 'isolated-sqlite-rehearsal', 'files_checked': len(expected_files),
                  'backup_at': manifest['created_at'], 'current_access_observed_at': ledger['observed_at'],
                  'sessions_invalidated': True, 'formal_sqlserver_ad': 'NOT VERIFIED'}
        (target_dir / 'restore-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (target_dir / 'recovered-settings.json').write_text(json.dumps({'APP_MODE': 'test',
            'DATABASE_URL': 'sqlite:///' + (target_dir / 'database.sqlite').as_posix(),
            'STORAGE_PATH': str(target_dir / 'attachments'), 'formal_integration': 'NOT VERIFIED'}, indent=2), encoding='utf-8')
        (target_dir / 'RESTORE_BLOCKED').unlink()
        return report
    finally:
        engine.dispose()
