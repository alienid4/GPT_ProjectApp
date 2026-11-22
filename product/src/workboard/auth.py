"""Server sessions and LDAPS authentication; test login is a separate provider."""
import hashlib
import hmac
import json
import re
import secrets
import ssl
from datetime import timedelta
from functools import wraps
from uuid import UUID

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from .errors import DomainError, require
from .models import LoginSession, LoginThrottle, Person, utcnow
from .policy import ROLE_CAPABILITIES, enabled

auth = Blueprint("auth", __name__, url_prefix="/api/auth")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def limit_login(engine):
    """Database-backed, cross-worker throttle committed before authentication.

    Only hashes of normalized identifiers are retained, never passwords.
    Failed authentication cannot roll back its own attempt counter.
    """
    now = utcnow()
    bucket = int(now.timestamp()) // 300
    data = request.get_json(silent=True)
    username = data.get("username", "") if isinstance(data, dict) else ""
    username = username.casefold()[:120] if isinstance(username, str) else "invalid"
    limits = [("account:" + username, 20), ("ip:" + (request.remote_addr or "unknown"), 100)]
    try:
        with engine.begin() as connection:
            connection.execute(delete(LoginThrottle).where(LoginThrottle.expires_at < now))
            for value, maximum in limits:
                key = digest(str(bucket) + value)
                count = connection.execute(update(LoginThrottle).where(LoginThrottle.key == key, LoginThrottle.attempts < maximum)
                                           .values(attempts=LoginThrottle.attempts + 1)).rowcount
                require(count in {0, 1}, "登入服務暫時無法使用。", 503, "throttle_unavailable")
                if count == 0:
                    require(connection.execute(select(LoginThrottle.key).where(LoginThrottle.key == key)).first() is None,
                            "登入嘗試過於頻繁，請稍後再試。", 429, "login_rate_limited")
                    connection.execute(insert(LoginThrottle).values(key=key, attempts=1, expires_at=now + timedelta(minutes=10)))
    except IntegrityError:
        raise DomainError("登入嘗試過於頻繁，請稍後再試。", 429, "login_rate_limited") from None


class TestIdentityProvider:
    def __init__(self, mode):
        if mode != "test":
            raise RuntimeError("Test identity provider requires explicit test mode.")

    def authenticate(self, db, username, password):
        # This provider is intentionally passwordless and only accepts preseeded
        # synthetic identities. It is never a fallback for LDAPS failure.
        person = db.scalar(select(Person).where(Person.username == username,
                                                Person.directory_id.like("test:%")))
        require(enabled(person), "登入失敗。", 401, "authentication_failed")
        return person

    def revalidate(self, db, person):
        return enabled(person) and person.directory_id.startswith("test:")


class LdapsIdentityProvider:
    def __init__(self, config):
        self.config = config
        required = ["LDAP_HOST", "LDAP_BASE_DN", "LDAP_BIND_DN", "LDAP_BIND_PASSWORD", "LDAP_ROLE_GROUPS", "LDAP_CA_FILE"]
        if any(not config.get(key) for key in required):
            raise RuntimeError("LDAPS configuration is incomplete; test login will not be enabled.")
        self.groups = config["LDAP_ROLE_GROUPS"]
        if not isinstance(self.groups, dict) or not self.groups or any(role not in ROLE_CAPABILITIES for role in self.groups):
            raise RuntimeError("LDAP role group mapping is invalid.")

    def connection(self, username, password):
        from ldap3 import Connection, Server, Tls
        server = Server(self.config["LDAP_HOST"], port=636, use_ssl=True,
                        tls=Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=self.config.get("LDAP_CA_FILE")),
                        connect_timeout=5)
        return Connection(server, user=username, password=password, auto_bind=True,
                          receive_timeout=5, raise_exceptions=True, auto_referrals=False)

    def lookup(self, filter_value):
        from ldap3 import SUBTREE
        conn = None
        try:
            conn = self.connection(self.config["LDAP_BIND_DN"], self.config["LDAP_BIND_PASSWORD"])
            conn.search(self.config["LDAP_BASE_DN"], "(&(objectClass=user)" + filter_value + ")",
                        search_scope=SUBTREE, attributes=["objectGUID", "sAMAccountName", "displayName", "memberOf", "userAccountControl"],
                        size_limit=2)
            if len(conn.entries) != 1:
                return None
            entry = conn.entries[0]
            attrs = entry.entry_attributes_as_dict
            groups = {str(x).casefold() for x in attrs.get("memberOf", [])}
            roles = [role for role, group in self.groups.items() if str(group).casefold() in groups]
            disabled = int(attrs.get("userAccountControl", [2])[0]) & 2
            if disabled or not roles:
                return None
            raw_guid = entry.entry_raw_attributes["objectGUID"][0]
            return {"id": str(UUID(bytes_le=raw_guid)), "dn": entry.entry_dn,
                    "username": str(attrs["sAMAccountName"][0]),
                    "name": str(attrs.get("displayName", attrs["sAMAccountName"])[0]), "roles": roles}
        except Exception as exc:
            raise DomainError("目錄服務暫時無法使用。", 503, "directory_unavailable") from None
        finally:
            if conn is not None:
                conn.unbind()

    def authenticate(self, db, username, password):
        from ldap3.core.exceptions import LDAPBindError
        from ldap3.utils.conv import escape_filter_chars
        require(bool(password) and len(password) <= 1024, "登入失敗。", 401, "authentication_failed")
        require(bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", username)), "登入失敗。", 401, "authentication_failed")
        entry = self.lookup("(sAMAccountName=" + escape_filter_chars(username) + ")")
        require(entry, "登入失敗。", 401, "authentication_failed")
        conn = None
        try:
            conn = self.connection(entry["dn"], password)
        except LDAPBindError:
            raise DomainError("登入失敗。", 401, "authentication_failed") from None
        except Exception:
            raise DomainError("目錄服務暫時無法使用。", 503, "directory_unavailable") from None
        finally:
            if conn is not None:
                conn.unbind()
        person = db.scalar(select(Person).where(Person.directory_id == entry["id"]))
        if person is None:
            person = Person(directory_id=entry["id"], username=entry["username"], name=entry["name"],
                            department=self.config["DEPARTMENT"], role=entry["roles"][0], roles_json=json.dumps(entry["roles"]))
            db.add(person)
            db.flush()
        require(person.active, "帳號未啟用。", 403, "account_disabled")
        self.apply_entry(person, entry)
        return person

    def apply_entry(self, person, entry):
        person.directory_active = True
        person.name = entry["name"]
        person.username = entry["username"]
        person.role = entry["roles"][0]
        person.roles_json = json.dumps(entry["roles"])

    def revalidate(self, db, person):
        if not person.active or person.directory_id.startswith("test:"):
            return False
        try:
            escaped = "".join("\\%02x" % byte for byte in UUID(person.directory_id).bytes_le)
        except ValueError:
            return False
        entry = self.lookup("(objectGUID=" + escaped + ")")
        if entry is None:
            person.directory_active = False
            return False
        self.apply_entry(person, entry)
        return True


def load_identity(revalidate=True):
    g.user = None
    g.login_session = None
    token = request.cookies.get("workboard_session", "")
    if not token or len(token) > 200:
        return
    session = g.db.get(LoginSession, digest(token))
    if session is None or session.expires_at <= utcnow():
        return
    person = g.db.get(Person, session.person_id)
    if not enabled(person) or person.auth_epoch != session.auth_epoch:
        return
    if revalidate and not current_app.extensions["identity_provider"].revalidate(g.db, person):
        return
    g.user, g.login_session = person, session


def authenticated(func):
    @wraps(func)
    def guarded(*args, **kwargs):
        require(g.user is not None, "請先登入。", 401, "authentication_required")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("X-CSRF-Token", "")
            require(len(csrf) <= 200 and hmac.compare_digest(digest(csrf), g.login_session.csrf_hash),
                    "驗證已失效，請重新載入。", 403, "csrf_failed")
        return func(*args, **kwargs)
    return guarded


def person_payload(person):
    return {"id": person.id, "username": person.username, "name": person.name,
            "department": person.department, "role": person.role}


@auth.get("/status")
def status():
    csrf = secrets.token_urlsafe(32)
    if g.user:
        result = {"user": person_payload(g.user), "csrf": g.login_session.csrf_token, "mode": current_app.config["APP_MODE"]}
        return jsonify(result)
    response = jsonify({"user": None, "csrf": csrf, "mode": current_app.config["APP_MODE"]})
    response.set_cookie("workboard_prelogin", csrf, httponly=True, samesite="Strict",
                        secure=current_app.config["COOKIE_SECURE"], max_age=600)
    return response


@auth.post("/login")
def login():
    csrf, cookie = request.headers.get("X-CSRF-Token", ""), request.cookies.get("workboard_prelogin", "")
    require(bool(csrf) and bool(cookie) and len(csrf) <= 200 and hmac.compare_digest(csrf, cookie),
            "驗證已失效，請重新載入。", 403, "csrf_failed")
    data = request.get_json(silent=True) or {}
    require(isinstance(data, dict), "登入資料格式錯誤。")
    username, password = data.get("username", ""), data.get("password", "")
    require(isinstance(username, str) and isinstance(password, str), "登入資料格式錯誤。")
    person = current_app.extensions["identity_provider"].authenticate(g.db, username, password)
    token, next_csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    previous = request.cookies.get("workboard_session")
    if previous:
        g.db.execute(delete(LoginSession).where(LoginSession.token_hash == digest(previous)))
    g.db.add(LoginSession(token_hash=digest(token), person_id=person.id, auth_epoch=person.auth_epoch,
                          csrf_hash=digest(next_csrf), csrf_token=next_csrf, expires_at=utcnow() + timedelta(hours=8)))
    response = jsonify({"user": person_payload(person), "csrf": next_csrf})
    response.set_cookie("workboard_session", token, httponly=True, samesite="Strict",
                        secure=current_app.config["COOKIE_SECURE"], max_age=8 * 3600)
    response.delete_cookie("workboard_prelogin", secure=current_app.config["COOKIE_SECURE"], samesite="Strict")
    return response


@auth.post("/logout")
@authenticated
def logout():
    g.db.delete(g.login_session)
    response = jsonify({"ok": True})
    response.delete_cookie("workboard_session", secure=current_app.config["COOKIE_SECURE"], samesite="Strict")
    return response
