from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


AUTH_STATE_LOCK = Lock()
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
INSTANCE_ID_PATTERN = re.compile(r"^i-[0-9a-fA-F]{8,17}$")
PHONE_DIGITS_PATTERN = re.compile(r"^\+?[0-9][0-9\s\-()]{7,20}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    return value.strip()


def comparable_phone(value: str) -> str:
    raw = normalize_phone(value)
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if digits.startswith("00"):
        digits = f"+{digits[2:]}"
    if digits and not digits.startswith("+"):
        digits = f"+{digits}"
    return digits


def default_auth_state() -> dict:
    return {
        "users": {},
        "email_index": {},
        "sessions": {},
        "phone_challenges": {},
    }


def read_auth_state(path: Path) -> dict:
    if not path.exists():
        return default_auth_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_auth_state()
    if not isinstance(payload, dict):
        return default_auth_state()
    payload.setdefault("users", {})
    payload.setdefault("email_index", {})
    payload.setdefault("sessions", {})
    payload.setdefault("phone_challenges", {})
    return payload


def ensure_dir_and_save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def password_hash(password: str, salt: str | None = None, iterations: int = 240000) -> str:
    safe_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        safe_salt.encode("utf-8"),
        iterations,
    )
    return f"pbkdf2_sha256${iterations}${safe_salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
    except (ValueError, TypeError):
        return False

    candidate = password_hash(password, salt=salt, iterations=iterations)
    return hmac.compare_digest(candidate, encoded)


def parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value)


def request_json(url: str, method: str = "GET", payload: dict | None = None, headers: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "voice-layer-control-plane/1.0",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    request = Request(url, data=body, headers=request_headers, method=method)
    with urlopen(request, timeout=20) as response:
        raw = response.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def twilio_config() -> tuple[str, str, str] | None:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
    if not account_sid or not auth_token or not from_number:
        return None
    return account_sid, auth_token, from_number


def send_twilio_sms(phone: str, code: str) -> dict:
    config = twilio_config()
    if config is None:
        return {
            "delivery": "demo",
            "sent": False,
            "reason": "twilio_not_configured",
            "demo_code": code,
        }

    account_sid, auth_token, from_number = config
    auth_bytes = f"{account_sid}:{auth_token}".encode("utf-8")
    auth_header = base64.b64encode(auth_bytes).decode("ascii")
    body = urlencode(
        {
            "To": phone,
            "From": from_number,
            "Body": f"Voice Layer verification code: {code}",
        }
    ).encode("utf-8")
    request = Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data=body,
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "voice-layer-control-plane/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        response.read()
    return {
        "delivery": "sms",
        "sent": True,
        "reason": "twilio_sent",
    }


def fetch_github_profile(username: str) -> dict:
    sanitized = username.strip().lstrip("@")
    if not sanitized:
        raise ValueError("GitHub username is required.")
    return request_json(f"https://api.github.com/users/{sanitized}")


def lookup_aws_with_cli(region: str, instance_id: str) -> dict | None:
    if not shutil.which("aws"):
        return None

    command = [
        "aws",
        "ec2",
        "describe-instances",
        "--region",
        region,
        "--instance-ids",
        instance_id,
        "--output",
        "json",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        return {
            "verified": False,
            "reason": (result.stderr or result.stdout or "aws_cli_failed").strip(),
        }

    payload = json.loads(result.stdout)
    reservations = payload.get("Reservations") or []
    for reservation in reservations:
        for instance in reservation.get("Instances") or []:
            if instance.get("InstanceId") == instance_id:
                return {
                    "verified": True,
                    "reason": "aws_cli",
                    "instance_state": (instance.get("State") or {}).get("Name", ""),
                    "public_ip": instance.get("PublicIpAddress", ""),
                    "public_dns": instance.get("PublicDnsName", ""),
                    "private_ip": instance.get("PrivateIpAddress", ""),
                }
    return {
        "verified": False,
        "reason": "instance_not_found",
    }


def lookup_aws_instance(region: str, instance_id: str) -> dict:
    try:
        return lookup_aws_with_cli(region, instance_id) or {
            "verified": False,
            "reason": "aws_cli_unavailable",
        }
    except Exception as exc:
        return {
            "verified": False,
            "reason": f"aws_lookup_error:{type(exc).__name__}",
        }


class AccountService:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.state = read_auth_state(state_path)
        self.cookie_name = os.environ.get("AUTH_COOKIE_NAME", "voice_layer_session").strip() or "voice_layer_session"
        self.cookie_secure = os.environ.get("AUTH_COOKIE_SECURE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.cookie_samesite = os.environ.get("AUTH_COOKIE_SAMESITE", "Lax").strip() or "Lax"
        self.demo_name = os.environ.get("DEMO_ACCOUNT_NAME", "Demo Operator").strip() or "Demo Operator"
        self.demo_email = normalize_email(
            os.environ.get("DEMO_ACCOUNT_EMAIL", "demo@voicelayer.local")
        )
        self.demo_password = os.environ.get("DEMO_ACCOUNT_PASSWORD", "demo123")
        self.seed_demo_account()

    def save_locked(self) -> None:
        ensure_dir_and_save(self.state_path, self.state)

    def seed_demo_account(self) -> None:
        with AUTH_STATE_LOCK:
            email_index = self.state.setdefault("email_index", {})
            users = self.state.setdefault("users", {})
            existing_id = email_index.get(self.demo_email)
            if existing_id and existing_id in users:
                user = users[existing_id]
                if not user.get("password_hash"):
                    user["password_hash"] = password_hash(self.demo_password)
                    user["updated_at"] = utc_now()
                    self.save_locked()
                return

            user_id = secrets.token_hex(12)
            now = utc_now()
            users[user_id] = {
                "id": user_id,
                "name": self.demo_name,
                "email": self.demo_email,
                "password_hash": password_hash(self.demo_password),
                "phone": "",
                "phone_verified": False,
                "github": {
                    "connected": False,
                    "username": "",
                    "connected_at": "",
                    "profile_url": "",
                    "avatar_url": "",
                    "name": "",
                },
                "aws_connections": [],
                "created_at": now,
                "updated_at": now,
                "last_login_at": "",
            }
            email_index[self.demo_email] = user_id
            self.save_locked()

    def demo_credentials(self) -> dict:
        return {
            "name": self.demo_name,
            "email": self.demo_email,
            "password": self.demo_password,
        }

    def cookie_header(self, token: str, max_age: int = 60 * 60 * 24 * 30) -> str:
        parts = [
            f"{self.cookie_name}={token}",
            "Path=/",
            f"Max-Age={max_age}",
            "HttpOnly",
            f"SameSite={self.cookie_samesite}",
        ]
        if self.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def clear_cookie_header(self) -> str:
        parts = [
            f"{self.cookie_name}=",
            "Path=/",
            "Max-Age=0",
            "HttpOnly",
            f"SameSite={self.cookie_samesite}",
        ]
        if self.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def session_token_from_cookie(self, cookie_header: str | None) -> str:
        if not cookie_header:
            return ""
        parts = [part.strip() for part in cookie_header.split(";")]
        prefix = f"{self.cookie_name}="
        for part in parts:
            if part.startswith(prefix):
                return part[len(prefix) :].strip()
        return ""

    def public_user(self, user: dict) -> dict:
        github = user.get("github") or {}
        return {
            "id": user.get("id", ""),
            "isAuthenticated": True,
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "phone": user.get("phone", ""),
            "phoneVerified": bool(user.get("phone_verified")),
            "github": {
                "connected": bool(github.get("connected")),
                "username": github.get("username", ""),
                "connectedAt": github.get("connected_at", ""),
                "profileUrl": github.get("profile_url", ""),
                "avatarUrl": github.get("avatar_url", ""),
                "name": github.get("name", ""),
            },
            "awsConnections": json.loads(json.dumps(user.get("aws_connections") or [])),
            "lastLoginAt": user.get("last_login_at", ""),
        }

    def current_user(self, cookie_header: str | None) -> dict | None:
        token = self.session_token_from_cookie(cookie_header)
        if not token:
            return None
        with AUTH_STATE_LOCK:
            sessions = self.state.setdefault("sessions", {})
            session = sessions.get(token)
            if not session:
                return None
            expires_at = session.get("expires_at", "")
            if expires_at and parse_iso8601(expires_at) <= datetime.now(timezone.utc):
                sessions.pop(token, None)
                self.save_locked()
                return None
            user_id = session.get("user_id", "")
            user = self.state.setdefault("users", {}).get(user_id)
            if not user:
                sessions.pop(token, None)
                self.save_locked()
                return None
            session["last_seen_at"] = utc_now()
            self.save_locked()
            return self.public_user(user)

    def user_by_phone(self, phone: str) -> dict | None:
        target = comparable_phone(phone)
        if not target:
            return None
        with AUTH_STATE_LOCK:
            users = self.state.setdefault("users", {})
            for user in users.values():
                if not bool(user.get("phone_verified")):
                    continue
                stored = comparable_phone(str(user.get("phone", "")))
                if stored and stored == target:
                    return self.public_user(user)
        return None

    def require_user(self, cookie_header: str | None) -> tuple[dict, dict]:
        token = self.session_token_from_cookie(cookie_header)
        if not token:
            raise PermissionError("You must be logged in.")
        with AUTH_STATE_LOCK:
            return self.require_user_locked(token)

    def require_user_locked(self, token: str) -> tuple[dict, dict]:
        session = self.state.setdefault("sessions", {}).get(token)
        if not session:
            raise PermissionError("Your session is invalid.")
        user = self.state.setdefault("users", {}).get(session.get("user_id", ""))
        if not user:
            raise PermissionError("Your user record was not found.")
        return user, session

    def create_session_locked(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        self.state.setdefault("sessions", {})[token] = {
            "id": secrets.token_hex(10),
            "user_id": user_id,
            "created_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "expires_at": (now + timedelta(days=30)).isoformat(),
        }
        return token

    def signup(self, name: str, email: str, password: str) -> tuple[dict, str]:
        normalized_email = normalize_email(email)
        if not name.strip():
            raise ValueError("Name is required.")
        if not EMAIL_PATTERN.match(normalized_email):
            raise ValueError("A valid email is required.")
        if len(password) < 6:
            raise ValueError("Password must be at least 6 characters.")

        with AUTH_STATE_LOCK:
            email_index = self.state.setdefault("email_index", {})
            if normalized_email in email_index:
                raise ValueError("An account with that email already exists.")
            user_id = secrets.token_hex(12)
            now = utc_now()
            self.state.setdefault("users", {})[user_id] = {
                "id": user_id,
                "name": name.strip(),
                "email": normalized_email,
                "password_hash": password_hash(password),
                "phone": "",
                "phone_verified": False,
                "github": {
                    "connected": False,
                    "username": "",
                    "connected_at": "",
                    "profile_url": "",
                    "avatar_url": "",
                    "name": "",
                },
                "aws_connections": [],
                "created_at": now,
                "updated_at": now,
                "last_login_at": now,
            }
            email_index[normalized_email] = user_id
            token = self.create_session_locked(user_id)
            self.save_locked()
            return self.public_user(self.state["users"][user_id]), token

    def login(self, email: str, password: str) -> tuple[dict, str]:
        normalized_email = normalize_email(email)
        with AUTH_STATE_LOCK:
            user_id = self.state.setdefault("email_index", {}).get(normalized_email)
            if not user_id:
                raise PermissionError("Invalid email or password.")
            user = self.state.setdefault("users", {}).get(user_id)
            if not user or not verify_password(password, user.get("password_hash", "")):
                raise PermissionError("Invalid email or password.")
            user["last_login_at"] = utc_now()
            user["updated_at"] = utc_now()
            token = self.create_session_locked(user_id)
            self.save_locked()
            return self.public_user(user), token

    def logout(self, cookie_header: str | None) -> None:
        token = self.session_token_from_cookie(cookie_header)
        if not token:
            return
        with AUTH_STATE_LOCK:
            self.state.setdefault("sessions", {}).pop(token, None)
            self.save_locked()

    def update_profile(self, cookie_header: str | None, name: str, email: str, phone: str) -> dict:
        normalized_email = normalize_email(email)
        normalized_phone = normalize_phone(phone)
        if not name.strip():
            raise ValueError("Name is required.")
        if not EMAIL_PATTERN.match(normalized_email):
            raise ValueError("A valid email is required.")
        if normalized_phone and not PHONE_DIGITS_PATTERN.match(normalized_phone):
            raise ValueError("Phone number format looks invalid.")

        with AUTH_STATE_LOCK:
            user, _session = self.require_user_locked(self.session_token_from_cookie(cookie_header))
            users = self.state.setdefault("users", {})
            email_index = self.state.setdefault("email_index", {})
            current = users[user["id"]]
            existing_id = email_index.get(normalized_email)
            if existing_id and existing_id != current["id"]:
                raise ValueError("That email is already in use.")

            old_email = current.get("email", "")
            if old_email != normalized_email:
                email_index.pop(old_email, None)
                email_index[normalized_email] = current["id"]

            if current.get("phone", "") != normalized_phone:
                current["phone_verified"] = False

            current["name"] = name.strip()
            current["email"] = normalized_email
            current["phone"] = normalized_phone
            current["updated_at"] = utc_now()
            self.save_locked()
            return self.public_user(current)

    def connect_github(self, cookie_header: str | None, username: str) -> dict:
        profile = fetch_github_profile(username)
        login = str(profile.get("login", "")).strip()
        if not login:
            raise ValueError("GitHub account not found.")

        with AUTH_STATE_LOCK:
            user, _session = self.require_user_locked(self.session_token_from_cookie(cookie_header))
            current = self.state.setdefault("users", {})[user["id"]]
            current["github"] = {
                "connected": True,
                "username": login,
                "connected_at": utc_now(),
                "profile_url": str(profile.get("html_url", "")).strip(),
                "avatar_url": str(profile.get("avatar_url", "")).strip(),
                "name": str(profile.get("name", "")).strip(),
            }
            current["updated_at"] = utc_now()
            self.save_locked()
            return self.public_user(current)

    def disconnect_github(self, cookie_header: str | None) -> dict:
        with AUTH_STATE_LOCK:
            user, _session = self.require_user_locked(self.session_token_from_cookie(cookie_header))
            current = self.state.setdefault("users", {})[user["id"]]
            current["github"] = {
                "connected": False,
                "username": "",
                "connected_at": "",
                "profile_url": "",
                "avatar_url": "",
                "name": "",
            }
            current["updated_at"] = utc_now()
            self.save_locked()
            return self.public_user(current)

    def send_phone_code(self, cookie_header: str | None, phone: str) -> dict:
        normalized_phone = normalize_phone(phone)
        if not PHONE_DIGITS_PATTERN.match(normalized_phone):
            raise ValueError("Enter a valid phone number.")

        with AUTH_STATE_LOCK:
            user, _session = self.require_user_locked(self.session_token_from_cookie(cookie_header))
            current = self.state.setdefault("users", {})[user["id"]]
            code = f"{secrets.randbelow(1000000):06d}"
            challenge_id = secrets.token_hex(12)
            now = datetime.now(timezone.utc)
            challenge = {
                "id": challenge_id,
                "user_id": current["id"],
                "phone": normalized_phone,
                "code": code,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=10)).isoformat(),
                "verified_at": "",
            }
            self.state.setdefault("phone_challenges", {})[challenge_id] = challenge
            current["phone"] = normalized_phone
            current["phone_verified"] = False
            current["updated_at"] = utc_now()
            self.save_locked()

        delivery = send_twilio_sms(normalized_phone, code)
        return {
            "challengeId": challenge_id,
            "phone": normalized_phone,
            "expiresAt": challenge["expires_at"],
            **delivery,
        }

    def verify_phone_code(self, cookie_header: str | None, phone: str, code: str) -> dict:
        normalized_phone = normalize_phone(phone)
        clean_code = code.strip()
        if not clean_code:
            raise ValueError("Verification code is required.")

        with AUTH_STATE_LOCK:
            user, _session = self.require_user_locked(self.session_token_from_cookie(cookie_header))
            current = self.state.setdefault("users", {})[user["id"]]
            challenges = self.state.setdefault("phone_challenges", {})
            latest_match = None
            for challenge in challenges.values():
                if challenge.get("user_id") != current["id"]:
                    continue
                if challenge.get("phone") != normalized_phone:
                    continue
                if latest_match is None or challenge.get("created_at", "") > latest_match.get("created_at", ""):
                    latest_match = challenge

            if latest_match is None:
                raise ValueError("No verification code was sent for that phone number.")
            if latest_match.get("verified_at"):
                raise ValueError("That code has already been used.")
            expires_at = parse_iso8601(latest_match["expires_at"])
            if expires_at <= datetime.now(timezone.utc):
                raise ValueError("That verification code has expired.")
            if latest_match.get("code") != clean_code:
                raise ValueError("Verification code is incorrect.")

            latest_match["verified_at"] = utc_now()
            current["phone"] = normalized_phone
            current["phone_verified"] = True
            current["updated_at"] = utc_now()
            self.save_locked()
            return self.public_user(current)

    def add_aws_connection(
        self,
        cookie_header: str | None,
        label: str,
        instance_id: str,
        region: str,
        host: str,
    ) -> dict:
        clean_instance_id = instance_id.strip()
        if not label.strip():
            raise ValueError("Label is required.")
        if not INSTANCE_ID_PATTERN.match(clean_instance_id):
            raise ValueError("AWS instance id must look like i-xxxxxxxx.")
        clean_region = region.strip() or "us-east-1"
        clean_host = host.strip()
        lookup = lookup_aws_instance(clean_region, clean_instance_id)
        if lookup.get("verified") and not clean_host:
            clean_host = lookup.get("public_dns") or lookup.get("public_ip") or clean_host

        with AUTH_STATE_LOCK:
            user, _session = self.require_user_locked(self.session_token_from_cookie(cookie_header))
            current = self.state.setdefault("users", {})[user["id"]]
            connections = current.setdefault("aws_connections", [])
            for existing in connections:
                if existing.get("instanceId") == clean_instance_id and existing.get("region") == clean_region:
                    raise ValueError("That AWS instance is already connected.")

            connections.insert(
                0,
                {
                    "id": secrets.token_hex(10),
                    "label": label.strip(),
                    "instanceId": clean_instance_id,
                    "region": clean_region,
                    "host": clean_host,
                    "connectedAt": utc_now(),
                    "verified": bool(lookup.get("verified")),
                    "verificationReason": lookup.get("reason", ""),
                    "instanceState": lookup.get("instance_state", ""),
                    "publicIp": lookup.get("public_ip", ""),
                    "privateIp": lookup.get("private_ip", ""),
                },
            )
            current["updated_at"] = utc_now()
            self.save_locked()
            return self.public_user(current)

    def remove_aws_connection(self, cookie_header: str | None, connection_id: str) -> dict:
        with AUTH_STATE_LOCK:
            user, _session = self.require_user_locked(self.session_token_from_cookie(cookie_header))
            current = self.state.setdefault("users", {})[user["id"]]
            existing = current.setdefault("aws_connections", [])
            current["aws_connections"] = [
                connection for connection in existing if connection.get("id") != connection_id
            ]
            current["updated_at"] = utc_now()
            self.save_locked()
            return self.public_user(current)
