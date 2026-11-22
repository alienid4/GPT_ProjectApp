"""Private immutable blobs. No web-server alias or user-supplied path is used."""
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import zipfile
from pathlib import Path

from flask import current_app
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from .errors import DomainError, require
from .models import Document, DocumentVersion, Person, Sequence, VersionReader
from .policy import can_edit, can_read, can_read_version
from .services import audit, open_execution, person


MAX_FILE_BYTES = 50 * 1024 * 1024
TYPES = {'.txt': 'text/plain', '.md': 'text/markdown', '.csv': 'text/csv', '.pdf': 'application/pdf',
         '.doc': 'application/msword', '.xls': 'application/vnd.ms-excel', '.ppt': 'application/vnd.ms-powerpoint',
         '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
         '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
         '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
         '.zip': 'application/zip', '.7z': 'application/x-7z-compressed', '.rar': 'application/vnd.rar'}


def classify(filename, contents):
    require(isinstance(filename, str) and 0 < len(filename) <= 255 and not re.search(r'[\x00-\x1f\\/:]', filename), "檔名格式不符。")
    suffix = Path(filename).suffix.lower()
    require(suffix in TYPES, "不支援此檔案類型。")
    require(0 < len(contents) <= MAX_FILE_BYTES, "檔案須介於 1 byte 與 50 MiB。")
    if suffix in {'.txt', '.md', '.csv'}:
        try:
            text = contents.decode('utf-8-sig')
            require('\x00' not in text, "文字檔內容不符。")
        except UnicodeDecodeError:
            raise DomainError("文字檔請使用 UTF-8 編碼。") from None
    elif suffix == '.pdf':
        require(contents.startswith(b'%PDF-'), "PDF 檔案格式不符。")
    elif suffix in {'.doc', '.xls', '.ppt'}:
        require(contents.startswith(bytes.fromhex('d0cf11e0a1b11ae1')), "Office 檔案格式不符。")
    elif suffix == '.7z':
        require(contents.startswith(bytes.fromhex('377abcaf271c')), "壓縮檔格式不符。")
    elif suffix == '.rar':
        require(contents.startswith(b'Rar!\x1a\x07'), "壓縮檔格式不符。")
    else:
        require(contents.startswith(b'PK'), "Office／ZIP 檔案格式不符。")
        try:
            # Read only the directory; never decompress or execute entries.
            with zipfile.ZipFile(io.BytesIO(contents)) as archive:
                entries = archive.infolist()
                require(len(entries) <= 6000 and sum(e.file_size for e in entries) <= 500 * 1024 * 1024,
                        "壓縮檔內容超過檢查限制。")
                if suffix != '.zip':
                    prefix = {'.docx': 'word/', '.xlsx': 'xl/', '.pptx': 'ppt/'}[suffix]
                    names = {e.filename for e in entries}
                    require('[Content_Types].xml' in names and any(n.startswith(prefix) for n in names), "Office 類型與內容不符。")
        except zipfile.BadZipFile:
            raise DomainError("壓縮檔格式不符。") from None
    return TYPES[suffix]


def blob_path(key):
    require(isinstance(key, str) and re.fullmatch(r'[0-9a-f]{64}', key), "附件索引無效。", 503)
    root = Path(current_app.config['STORAGE_PATH']).resolve()
    path = root / key[:2] / key
    require(root in path.resolve().parents, "附件索引無效。", 503)
    return path


def scan(path):
    command = current_app.config['SCANNER_COMMAND']
    if not command:
        return 'test_only' if current_app.config['APP_MODE'] == 'test' else 'quarantined'
    try:
        argv = json.loads(command) if isinstance(command, str) else command
        require(isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv), "掃描設定無效。", 503)
        result = subprocess.run([*argv, str(path)], shell=False, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=90, check=False)
        return 'clean' if result.returncode == 0 else 'infected' if result.returncode == 1 else 'quarantined'
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return 'quarantined'


def permitted_scan(version):
    return version.scan_status == 'clean' or (version.scan_status == 'test_only' and current_app.config['APP_MODE'] == 'test')


def read_blob(version):
    require(not version.deleted and permitted_scan(version), "附件尚未通過檢查或已不可用。", 409, 'evidence_unavailable')
    path = blob_path(version.blob_key)
    try:
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == version.size, "附件佐證不可用。", 409, 'evidence_unavailable')
        contents = path.read_bytes()
    except OSError:
        raise DomainError("附件佐證不可用。", 409, 'evidence_unavailable') from None
    require(hashlib.sha256(contents).hexdigest() == version.sha256, "附件佐證不可用。", 409, 'evidence_unavailable')
    return contents


def pending_upload_path(key):
    blob_path(key)  # Apply the same opaque-key and containment checks.
    root = Path(current_app.config['STORAGE_PATH']).resolve()
    directory = root / '.pending-uploads'
    require(root in directory.resolve().parents, '附件索引無效。', 503)
    return directory / key


def record_pending_upload(key):
    marker = pending_upload_path(key)
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with marker.open('xb') as stream:
        os.chmod(marker, 0o600)
        stream.write(b'pending\n')
        stream.flush()
        os.fsync(stream.fileno())
    # Persist the journal directory entry before writing any attachment bytes.
    if hasattr(os, 'O_DIRECTORY'):
        for directory in [marker.parent, marker.parent.parent]:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def discard_uncommitted_uploads(keys, sessions):
    """Recheck committed metadata after the request transaction has closed.

    A failed/uncertain commit must never cause deletion of a committed version.
    On an unavailable database or storage, retain the file and emit an opaque
    key for operator reconciliation. Durable markers allow a later retry even
    after process termination; they are removed only after metadata is checked.
    """
    results = []
    for key in keys:
        try:
            with sessions() as verification:
                registered = verification.scalar(select(DocumentVersion.id).where(DocumentVersion.blob_key == key))
            if registered is None:
                blob_path(key).unlink(missing_ok=True)
            pending_upload_path(key).unlink(missing_ok=True)
            results.append({'key': key, 'status': 'registered' if registered else 'removed'})
        except (SQLAlchemyError, OSError, DomainError):
            current_app.logger.error('upload_reconciliation_required blob=%s', key)
            results.append({'key': key, 'status': 'pending'})
    return results


def reconcile_upload_journal(sessions, limit):
    directory = pending_upload_path('0' * 64).parent
    if not directory.exists():
        return []
    # The same mutation barrier as HTTP uploads prevents reconciling a file
    # whose owning request is still writing or waiting to commit.
    keys, results = [], []
    for marker in directory.iterdir():
        if re.fullmatch(r'[0-9a-f]{64}', marker.name) and marker.is_file() and not marker.is_symlink():
            keys.append(marker.name)
            if len(keys) >= limit:
                break
    for key in keys:
        # Release the barrier between files instead of blocking all writes for
        # the whole batch. Individual slow storage calls can still delay writes.
        with sessions.begin() as barrier:
            state = barrier.get(Sequence, 'restore_ready')
            require(state is None or state.value == 1, '還原尚未完成。', 503)
            count = barrier.execute(update(Sequence).where(Sequence.key == 'application_transaction')
                                    .values(value=Sequence.value)).rowcount
            require(count == 1, '資料結構尚未完成升級。', 503)
            results.extend(discard_uncommitted_uploads([key], sessions))
    return results


def upload(db, actor, task, file, document_id=None, readers=None):
    require(can_edit(db, actor, task), "無權上傳附件。", 403)
    open_execution(task, db)
    require(file is not None and file.filename, "請選擇附件。")
    contents = file.stream.read(MAX_FILE_BYTES + 1)
    mime = classify(file.filename, contents)
    if document_id:
        document = db.get(Document, document_id)
        require(document is not None and document.task_id == task.id, "找不到附件。", 404)
        prior = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.number.desc()))
        require(prior is not None and can_read_version(db, actor, task, prior), "找不到附件。", 404)
        # New versions may narrow, but cannot widen an existing version list.
        old_readers = set(db.scalars(select(VersionReader.person_id).where(VersionReader.version_id == prior.id))) if prior.restricted else None
        if old_readers is not None:
            readers = list(old_readers) if readers is None else readers
            require(isinstance(readers, list) and all(isinstance(pid, str) for pid in readers) and set(readers) <= old_readers,
                    "新版不可擴大附件名單。", 403)
    else:
        document = Document(task_id=task.id, title=file.filename)
        db.add(document)
        db.flush()
    if readers is not None:
        require(isinstance(readers, list) and 0 < len(readers) <= 100 and all(isinstance(pid, str) for pid in readers), "附件名單格式不符。")
        for pid in set(readers):
            candidate = person(db, actor, pid)
            require(can_read(db, candidate, task), "附件名單不可擴大工作讀權。", 403)
        require(actor.id in readers, "附件名單須包含上傳人。")
    key = secrets.token_hex(32)
    path = blob_path(key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with path.open('xb') as stream:
            # Register only files this operation actually created. An existing
            # filename collision must never become eligible for compensation.
            db.info.setdefault('created_blob_keys', set()).add(key)
            record_pending_upload(key)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
    except OSError:
        raise DomainError("附件空間暫時無法寫入。", 503, 'attachment_storage_unavailable') from None
    number = (db.scalar(select(func.max(DocumentVersion.number)).where(DocumentVersion.document_id == document.id)) or 0) + 1
    version = DocumentVersion(document_id=document.id, task_id=task.id, number=number, filename=file.filename,
                              blob_key=key, sha256=hashlib.sha256(contents).hexdigest(), size=len(contents), mime=mime,
                              author_id=actor.id, scan_status=scan(path), restricted=readers is not None)
    db.add(version)
    db.flush()
    if readers is not None:
        db.add_all([VersionReader(version_id=version.id, person_id=pid) for pid in set(readers)])
    # General events must not leak filenames for separately restricted versions.
    audit(db, actor, task, 'attachment_uploaded', after={'event': '新增附件版本'})
    return version


def version_payload(version):
    try:
        read_blob(version)
        available = True
    except (DomainError, OSError):
        available = False
    return {'id': version.id, 'document_id': version.document_id, 'number': version.number,
            'filename': version.filename, 'sha256': version.sha256, 'size': version.size,
            'scan_status': version.scan_status, 'available': available, 'restricted': version.restricted,
            'created_at': version.created_at.isoformat() + 'Z', 'expires_at': version.expires_at.isoformat() + 'Z'}
