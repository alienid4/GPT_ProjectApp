import json
import logging
import os
from pathlib import Path

import click
from flask import Flask, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError

from . import __version__
from .auth import (LdapsIdentityProvider, TestIdentityProvider, auth, authenticated, digest,
                   limit_login, load_identity, person_payload)
from .database import database
from .errors import DomainError
from .models import Base, Person, SecurityEvent, Sequence
from .policy import actor_roles, global_capability


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.update(
        APP_MODE=os.environ.get("APP_MODE", "production"),
        BUILD_ID=os.environ.get("WORKBOARD_BUILD", "development"),
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
        DEPARTMENT=os.environ.get("WORKBOARD_DEPARTMENT", "系統架構科"),
        MAX_CONTENT_LENGTH=52 * 1024 * 1024,
        TRUSTED_HOSTS=os.environ.get("WORKBOARD_HOSTS", "localhost,127.0.0.1").split(","),
        LDAP_HOST=os.environ.get("LDAP_HOST", ""),
        LDAP_BASE_DN=os.environ.get("LDAP_BASE_DN", ""),
        LDAP_BIND_DN=os.environ.get("LDAP_BIND_DN", ""),
        LDAP_BIND_PASSWORD=os.environ.get("LDAP_BIND_PASSWORD", ""),
        LDAP_CA_FILE=os.environ.get("LDAP_CA_FILE") or None,
        LDAP_ROLE_GROUPS=json.loads(os.environ.get("LDAP_ROLE_GROUPS", "{}")),
        STORAGE_PATH=os.environ.get("WORKBOARD_STORAGE", str(Path(app.instance_path) / "attachments")),
        SCANNER_COMMAND=os.environ.get("WORKBOARD_SCANNER", ""),
    )
    if overrides:
        app.config.update(overrides)
    mode = app.config["APP_MODE"]
    if not app.config["DATABASE_URL"]:
        raise RuntimeError("DATABASE_URL must be explicitly configured.")
    app.config["COOKIE_SECURE"] = mode == "production"
    engine, sessions = database(app.config["DATABASE_URL"], mode)
    app.extensions["engine"], app.extensions["sessions"] = engine, sessions
    app.extensions["identity_provider"] = (TestIdentityProvider(mode) if mode == "test"
                                             else LdapsIdentityProvider(app.config))
    app.register_blueprint(auth)
    from .api import api
    app.register_blueprint(api)
    from .reporting import reports
    app.register_blueprint(reports)
    from .governance import governance
    app.register_blueprint(governance)
    from .retention import retention
    app.register_blueprint(retention)

    @app.before_request
    def open_request():
        if engine.dialect.name == 'sqlite' and engine.url.database not in {None, '', ':memory:'}:
            directory = Path(engine.url.database).resolve().parent
            if any((directory / marker).exists() for marker in ['RESTORE_BLOCKED', 'INCOMPLETE']):
                raise DomainError('還原尚未通過核對，服務暫不開放。', 503, 'restore_not_ready')
        if request.endpoint == "auth.login":
            limit_login(engine)
        g.db = sessions()
        g.user, g.login_session = None, None
        if request.endpoint not in {'static', 'health'}:
            restore_state = g.db.get(Sequence, 'restore_ready')
            if restore_state is not None and restore_state.value != 1:
                raise DomainError('還原尚未通過核對，服務暫不開放。', 503, 'restore_not_ready')
        # Static assets and health checks do not depend on directory availability.
        if request.endpoint not in {"static", "health"}:
            # Directory I/O occurs BEFORE the mutation lock. Anonymous requests
            # and reads never obtain the global write lock. Current ACL queries
            # authorize reads when the request is serviced, not at login time.
            load_identity()
            if g.user is not None and request.method not in {'GET', 'HEAD', 'OPTIONS'} and not (request.endpoint or '').startswith('auth.'):
                g.db.flush()
                count = g.db.execute(update(Sequence).where(Sequence.key == 'application_transaction')
                                     .values(value=Sequence.value)).rowcount
                if not count:
                    if mode != 'test':
                        raise DomainError('資料結構尚未完成升級。', 503, 'migration_required')
                    g.db.add(Sequence(key='application_transaction', value=1))
                    g.db.flush()
                # A disable/role update may have won while we waited for the
                # lock. Expire the earlier identity snapshot and check again.
                g.db.expire_all()
                load_identity(revalidate=False)

    @app.after_request
    def close_transaction(response):
        if hasattr(g, "db"):
            if response.status_code < 400:
                try:
                    g.db.commit()
                except (IntegrityError, StaleDataError):
                    g.db.rollback()
                    response = jsonify({"error": "資料已變更，請重新載入。", "code": "conflict"})
                    response.status_code = 409
                except SQLAlchemyError:
                    g.db.rollback()
                    response = jsonify({"error": "資料暫時無法儲存。", "code": "storage_unavailable"})
                    response.status_code = 503
            else:
                g.db.rollback()
        response.headers["Cache-Control"] = "no-store"
        if response.status_code < 400 and getattr(g, 'cleanup_pending', None):
            from .retention import remove_committed_blob
            body = response.get_json()
            for item_id in g.cleanup_pending:
                try:
                    result = remove_committed_blob(item_id)
                    body['results'] = [result if r.get('resource_type') == result['resource_type'] and r.get('resource_id') == result['resource_id'] else r for r in body['results']]
                except (SQLAlchemyError, DomainError):
                    app.logger.error('cleanup_file_followup_failed item=%s', item_id)
            response.set_data(json.dumps(body, ensure_ascii=False))
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if mode == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if response.status_code in {401, 403, 404, 429, 503} or request.endpoint in {'auth.login', 'auth.logout'}:
            payload = response.get_json(silent=True) or {}
            entry = {'actor_id': getattr(getattr(g, 'user', None), 'id', None),
                     'route': request.endpoint or 'unmatched', 'outcome': response.status_code,
                     'code': str(payload.get('code', 'allowed' if response.status_code < 400 else 'denied'))[:80],
                     'address_hash': digest(request.remote_addr or 'unknown')}
            # Independent transaction: failed business operations cannot erase
            # security evidence; logging never includes body, query or password.
            try:
                with sessions.begin() as security_db:
                    security_db.add(SecurityEvent(**entry))
            except SQLAlchemyError:
                app.logger.error('security_event_persistence_failed %s', json.dumps(entry))
        return response

    @app.teardown_request
    def finish_request(_):
        if hasattr(g, "db"):
            keys = g.db.info.pop('created_blob_keys', set())
            g.db.close()
            if keys:
                from .storage import discard_uncommitted_uploads
                discard_uncommitted_uploads(keys, sessions)

    @app.errorhandler(DomainError)
    def domain_error(error):
        if hasattr(g, "db"):
            g.db.rollback()
        return jsonify({"error": error.message, "code": error.code}), error.status

    @app.errorhandler(StaleDataError)
    @app.errorhandler(IntegrityError)
    def conflict(_):
        g.db.rollback()
        return jsonify({"error": "資料已變更或不符合條件。", "code": "conflict"}), 409

    @app.errorhandler(SQLAlchemyError)
    def database_error(_):
        if hasattr(g, "db"):
            g.db.rollback()
        return jsonify({"error": "資料服務暫時無法使用。", "code": "storage_unavailable"}), 503

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "version": __version__, "mode": mode, "build": app.config["BUILD_ID"]})

    @app.get("/login")
    def login_page():
        identities = []
        if mode == "test":
            identities = list(g.db.scalars(select(Person).where(Person.directory_id.like("test:%"), Person.active.is_(True))))
        return render_template("login.html", version=__version__, mode=mode, identities=identities)

    @app.get("/")
    def home():
        if not g.user:
            return redirect(url_for("login_page"))
        return render_template("workboard.html", version=__version__, build=app.config["BUILD_ID"][:12], mode=mode, user=g.user)

    @app.get("/api/me")
    @authenticated
    def me():
        return jsonify({**person_payload(g.user), "roles": sorted(actor_roles(g.user)),
                        "capabilities": [cap for cap in ["department_view", "dispatch", "users", "settings", "templates", "cleanup", "backup", "capacity"] if global_capability(g.user, cap)]})

    @app.cli.command("init-test-db")
    def init_test_db():
        if mode != "test":
            raise click.ClickException("Production schema changes require the reviewed migration command.")
        Base.metadata.create_all(engine)
        click.echo("Test schema initialized; production integrations not verified.")

    @app.cli.command("seed-test-identities")
    def seed_test_identities():
        if mode != "test":
            raise click.ClickException("Synthetic identities are prohibited in production mode.")
        with sessions.begin() as db:
            for username, name, role in [("engineer-a", "陳工程師", "member"), ("engineer-b", "周工程師", "member"),
                                         ("chief", "林主管", "chief"), ("director", "部門主管", "director"),
                                         ("maintenance", "維護人員", "maintenance"), ("cleaner", "清理人員", "cleaner")]:
                if db.scalar(select(Person.id).where(Person.username == username)) is None:
                    db.add(Person(directory_id="test:" + username, username=username, name=name,
                                  department=app.config["DEPARTMENT"], role=role))
        click.echo("Synthetic test identities created; this is not AD authentication.")

    @app.cli.command('backup-test')
    @click.argument('destination', type=click.Path())
    def backup_test(destination):
        if mode != 'test':
            raise click.ClickException('This adapter cannot verify production SQL Server backup.')
        from .backup import create_backup
        result = create_backup(engine, app.config['STORAGE_PATH'], destination)
        click.echo(json.dumps({'adapter': result['adapter'], 'files': len(result['files']), 'formal_integration': 'NOT VERIFIED'}))

    @app.cli.command('restore-test')
    @click.argument('backup_dir', type=click.Path(exists=True))
    @click.argument('destination', type=click.Path())
    def restore_test(backup_dir, destination):
        if mode != 'test':
            raise click.ClickException('This adapter cannot verify production SQL Server recovery.')
        from .backup import restore_backup
        click.echo(json.dumps(restore_backup(backup_dir, destination, engine)))

    @app.cli.command('cleanup-retry')
    @click.option('--limit', default=100, type=click.IntRange(1, 1000))
    def cleanup_retry(limit):
        from .models import CleanupItem
        from .retention import remove_committed_blob
        with sessions() as db:
            ids = list(db.scalars(select(CleanupItem.id).where(CleanupItem.resource_type == 'version',
                CleanupItem.status.in_(['pending_file', 'failed_file'])).limit(limit)))
        for item_id in ids:
            click.echo(json.dumps(remove_committed_blob(item_id)))

    @app.cli.command('reconcile-uploads')
    @click.option('--limit', default=100, type=click.IntRange(1, 1000))
    def reconcile_uploads(limit):
        from .storage import reconcile_upload_journal
        try:
            results = reconcile_upload_journal(sessions, limit)
        except (DomainError, SQLAlchemyError, OSError) as error:
            app.logger.error('upload_journal_retry_failed type=%s', type(error).__name__)
            raise click.ClickException('附件補償暫時無法執行，請核對儲存與資料庫。') from None
        click.echo(json.dumps({'checked': len(results), 'pending': sum(r['status'] == 'pending' for r in results)}))
        if any(r['status'] == 'pending' for r in results):
            raise click.ClickException('附件補償仍有未完成項目，請核對儲存與資料庫。')

    return app
