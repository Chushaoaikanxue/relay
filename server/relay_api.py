#!/usr/bin/env python3
"""Small self-hosted control API and node-to-node transfer orchestrator for Relay."""

from __future__ import annotations

import hashlib
import hmac
import base64
import ipaddress
import json
import os
import re
import secrets
import signal
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from relay_transfer import TransferManager


API_PREFIX = "/relay/api"
SESSION_COOKIE = "relay_session"
DEFAULT_NODE_CREDENTIAL_NAME = "relay_ed25519"
DEFAULT_TASK_SIZE_LIMIT_BYTES = 100 * 1024 * 1024 * 1024
MAX_TASK_SIZE_LIMIT_GB = 1_048_576
DEFAULT_MAX_CONCURRENT_TRANSFERS = 4
DEFAULT_MAX_TRANSFERS_PER_NODE = 2
MAX_CONCURRENT_TRANSFERS = 16
MAX_TRANSFERS_PER_NODE = 8
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
TASK_ACTION_PATTERN = re.compile(r"^/relay/api/tasks/([0-9a-f-]{36})/(pause|resume|retry)$")
TASK_PATTERN = re.compile(r"^/relay/api/tasks/([0-9a-f-]{36})$")
NODE_ACTION_PATTERN = re.compile(r"^/relay/api/nodes/([0-9a-f-]{36})/(test|transfer-route)$")
NODE_PATTERN = re.compile(r"^/relay/api/nodes/([0-9a-f-]{36})$")
USER_ACTION_PATTERN = re.compile(r"^/relay/api/users/(\d+)/(reset-password)$")
SSH_USERNAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
HOSTNAME_PATTERN = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def clean_text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 格式不正确")
    value = value.strip()
    if required and not value:
        raise ValueError(f"请填写{field}")
    if len(value) > maximum or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{field}内容不合法")
    return value


def clean_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}格式不正确")
    value = value.strip()
    if not value.startswith("/") or value == "/" or len(value) > 2048:
        raise ValueError(f"{field}必须是非根目录的绝对路径")
    if any(ord(character) < 32 for character in value) or any(character in value for character in "\\\"'"):
        raise ValueError(f"{field}包含不支持的字符")
    if ".." in Path(value).parts:
        raise ValueError(f"{field}不能包含上级目录跳转")
    return value


def clean_schedule_at(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("预约时间格式不正确")
    try:
        scheduled = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("预约时间格式不正确") from exc
    if scheduled.tzinfo is None:
        raise ValueError("预约时间必须包含时区")
    scheduled = scheduled.astimezone(timezone.utc)
    if scheduled <= utc_now() + timedelta(seconds=30):
        raise ValueError("预约时间至少应晚于当前时间 30 秒")
    return scheduled.isoformat(timespec="seconds")


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000, dklen=32)
    return salt, digest


class RelayApp:
    def __init__(self, db_path: str, allowed_origin: str, secure_cookie: bool = True, *, enable_transfers: bool = True):
        self.db_path = db_path
        self.allowed_origin = allowed_origin.rstrip("/")
        self.secure_cookie = secure_cookie
        self.state_dir = Path(db_path).parent
        self.keys_dir = self.state_dir / "keys"
        self.known_hosts_path = self.state_dir / "known_hosts"
        self.login_lock = threading.Lock()
        self.login_attempts: dict[str, tuple[int, float, float]] = {}
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.keys_dir, 0o700)
        self.initialize_database()
        settings = self.transfer_settings()
        self.transfers = TransferManager(
            db_path, self.state_dir, self.known_hosts_path, enabled=enable_transfers,
            max_concurrent=settings["max_concurrent"], max_per_node=settings["max_per_node"],
        )

    def login_allowed(self, client_ip: str) -> bool:
        now = time.monotonic()
        with self.login_lock:
            count, window_started, blocked_until = self.login_attempts.get(client_ip, (0, now, 0.0))
            if blocked_until > now:
                return False
            if now - window_started > 900:
                self.login_attempts.pop(client_ip, None)
            return True

    def record_login_failure(self, client_ip: str) -> None:
        now = time.monotonic()
        with self.login_lock:
            count, window_started, _ = self.login_attempts.get(client_ip, (0, now, 0.0))
            if now - window_started > 900:
                count, window_started = 0, now
            count += 1
            blocked_until = now + 900 if count >= 5 else 0.0
            self.login_attempts[client_ip] = (count, window_started, blocked_until)

    def clear_login_failures(self, client_ip: str) -> None:
        with self.login_lock:
            self.login_attempts.pop(client_ip, None)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize_database(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS transfer_settings (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    max_concurrent INTEGER NOT NULL,
                    max_per_node INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO transfer_settings(id, max_concurrent, max_per_node, updated_at)
                VALUES (1, ?, ?, ?)
                """,
                (DEFAULT_MAX_CONCURRENT_TRANSFERS, DEFAULT_MAX_TRANSFERS_PER_NODE, iso_now()),
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    key_path TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    owner_id INTEGER NOT NULL REFERENCES users(id),
                    auth_type TEXT NOT NULL DEFAULT 'key' CHECK(auth_type IN ('key', 'password')),
                    password_path TEXT,
                    direct_host TEXT,
                    direct_port INTEGER
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY,
                    actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('online', 'offline', 'pending')),
                    host TEXT NOT NULL,
                    ssh_port INTEGER NOT NULL DEFAULT 22,
                    username TEXT NOT NULL,
                    transfer_host TEXT,
                    transfer_port INTEGER,
                    credential_id TEXT REFERENCES credentials(id),
                    last_error TEXT,
                    last_seen_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('transferring', 'queued', 'completed', 'failed', 'paused')),
                    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
                    total_bytes INTEGER,
                    transferred_bytes INTEGER NOT NULL DEFAULT 0,
                    progress_base_bytes INTEGER NOT NULL DEFAULT 0,
                    speed_bps INTEGER,
                    eta_seconds INTEGER,
                    file_count INTEGER,
                    error_message TEXT,
                    kind TEXT NOT NULL DEFAULT 'folder' CHECK(kind IN ('folder', 'archive', 'code')),
                    started_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    owner_id INTEGER NOT NULL REFERENCES users(id)
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner_updated ON tasks(owner_id, updated_at DESC)")
            self.ensure_column(db, "nodes", "credential_id", "TEXT REFERENCES credentials(id)")
            self.ensure_column(db, "nodes", "last_error", "TEXT")
            self.ensure_column(db, "nodes", "transfer_host", "TEXT")
            self.ensure_column(db, "nodes", "transfer_port", "INTEGER")
            self.ensure_column(db, "credentials", "auth_type", "TEXT NOT NULL DEFAULT 'key'")
            self.ensure_column(db, "credentials", "password_path", "TEXT")
            self.ensure_column(db, "users", "role", "TEXT NOT NULL DEFAULT 'user'")
            self.ensure_column(db, "tasks", "source_node_id", "TEXT REFERENCES nodes(id)")
            self.ensure_column(db, "tasks", "destination_node_id", "TEXT REFERENCES nodes(id)")
            self.ensure_column(db, "tasks", "source_path", "TEXT")
            self.ensure_column(db, "tasks", "destination_path", "TEXT")
            self.ensure_column(db, "tasks", "delete_enabled", "INTEGER NOT NULL DEFAULT 0")
            self.ensure_column(db, "tasks", "bandwidth_limit_kbps", "INTEGER")
            self.ensure_column(db, "tasks", "max_size_bytes", f"INTEGER NOT NULL DEFAULT {DEFAULT_TASK_SIZE_LIMIT_BYTES}")
            self.ensure_column(db, "tasks", "log_tail", "TEXT")
            self.ensure_column(db, "tasks", "completed_at", "TEXT")
            self.ensure_column(db, "tasks", "schedule_at", "TEXT")
            self.ensure_column(db, "tasks", "progress_base_bytes", "INTEGER NOT NULL DEFAULT 0")
            self.ensure_column(db, "tasks", "direct_host", "TEXT")
            self.ensure_column(db, "tasks", "direct_port", "INTEGER")
            self.ensure_column(db, "tasks", "verify_after_transfer", "INTEGER NOT NULL DEFAULT 0")
            self.ensure_column(db, "tasks", "verification_status", "TEXT")
            self.ensure_column(db, "tasks", "verification_message", "TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_connection ON nodes(host, ssh_port, username)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_source_node ON tasks(source_node_id, updated_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_destination_node ON tasks(destination_node_id, updated_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor_created ON audit_events(actor_id, created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC)")
            first_user = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
            if first_user:
                db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (first_user["id"],))
            db.execute("PRAGMA optimize")

    def transfer_settings(self) -> dict[str, int]:
        with self.connect() as db:
            row = db.execute(
                "SELECT max_concurrent, max_per_node FROM transfer_settings WHERE id = 1"
            ).fetchone()
        if not row:
            return {
                "max_concurrent": DEFAULT_MAX_CONCURRENT_TRANSFERS,
                "max_per_node": DEFAULT_MAX_TRANSFERS_PER_NODE,
            }
        return {"max_concurrent": row["max_concurrent"], "max_per_node": row["max_per_node"]}

    @staticmethod
    def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def store_private_key(self, private_key: str) -> tuple[str, str, str]:
        if not isinstance(private_key, str) or not (100 <= len(private_key) <= 32_768):
            raise ValueError("私钥内容为空或长度不正确")
        if "\x00" in private_key or "PRIVATE KEY-----" not in private_key:
            raise ValueError("私钥格式不正确")
        credential_id = str(uuid.uuid4())
        key_path = self.keys_dir / credential_id
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as key_file:
                key_file.write(private_key.rstrip() + "\n")
            os.chmod(key_path, 0o600)
            result = subprocess.run(
                ["/usr/bin/ssh-keygen", "-y", "-f", str(key_path)],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
            if result.returncode != 0 or not result.stdout.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
                raise ValueError("私钥无法读取；当前仅支持未设置口令的 SSH 私钥")
            public_parts = result.stdout.strip().split()
            public_blob = base64.b64decode(public_parts[1], validate=True)
            fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(public_blob).digest()).decode("ascii").rstrip("=")
            return credential_id, str(key_path), fingerprint
        except Exception:
            key_path.unlink(missing_ok=True)
            raise

    def store_password(self, password: Any) -> tuple[str, str]:
        password = clean_text(password, field="SSH 密码", maximum=512)
        if len(password) < 1:
            raise ValueError("SSH 密码不能为空")
        credential_id = str(uuid.uuid4())
        password_path = self.keys_dir / f"{credential_id}.password"
        fd = os.open(password_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as password_file:
                password_file.write(password)
            os.chmod(password_path, 0o600)
            return credential_id, str(password_path)
        except Exception:
            password_path.unlink(missing_ok=True)
            raise

    def ensure_askpass_helper(self) -> Path:
        helper = self.keys_dir / "relay-ssh-askpass"
        if not helper.exists():
            helper.write_text("#!/bin/sh\nset -eu\n/usr/bin/cat -- \"$RELAY_SSH_PASSWORD_FILE\"\n", encoding="utf-8")
            os.chmod(helper, 0o700)
        return helper

    def audit(self, actor_id: int | None, action: str, entity_type: str, entity_id: Any = None, detail: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit_events(actor_id, action, entity_type, entity_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (actor_id, action, entity_type, str(entity_id) if entity_id is not None else None, detail, iso_now()),
            )

    def default_node_credential(self, owner_id: int) -> tuple[sqlite3.Row, str]:
        with self.connect() as db:
            credential = db.execute(
                """
                SELECT id, name, fingerprint, key_path
                FROM credentials
                WHERE owner_id = ? AND name = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (owner_id, DEFAULT_NODE_CREDENTIAL_NAME),
            ).fetchone()
        if not credential:
            raise ValueError(f"未找到默认凭据 {DEFAULT_NODE_CREDENTIAL_NAME}")
        result = subprocess.run(
            ["/usr/bin/ssh-keygen", "-y", "-f", credential["key_path"]],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
        public_key = result.stdout.strip()
        if result.returncode != 0 or not public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
            raise ValueError("默认凭据不可读取")
        return credential, f"{public_key} relay-control"

    @staticmethod
    def validate_host(host: Any) -> str:
        if not isinstance(host, str):
            raise ValueError("节点地址格式不正确")
        host = host.strip()
        if not host or len(host) > 253 or "\x00" in host:
            raise ValueError("节点地址格式不正确")
        try:
            address = ipaddress.ip_address(host)
            if address.version != 4:
                raise ValueError("当前节点管理仅支持 IPv4 或主机名")
            return str(address)
        except ValueError:
            if not HOSTNAME_PATTERN.fullmatch(host):
                raise ValueError(f"节点地址不合法：{host}")
            return host.lower()

    def test_node_async(self, node_ids: list[str]) -> None:
        thread = threading.Thread(target=self._test_nodes, args=(node_ids,), daemon=True, name="relay-node-test")
        thread.start()

    def _test_nodes(self, node_ids: list[str]) -> None:
        for node_id in node_ids:
            with self.connect() as db:
                node = db.execute(
                    """
                    SELECT nodes.*, credentials.key_path, credentials.auth_type, credentials.password_path
                    FROM nodes JOIN credentials ON credentials.id = nodes.credential_id
                    WHERE nodes.id = ?
                    """,
                    (node_id,),
                ).fetchone()
            if not node:
                continue
            target = f"{node['username']}@{node['host']}"
            command = ["/usr/bin/ssh", "-F", "/dev/null", "-p", str(node["ssh_port"]),
                       "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
                       "-o", "StrictHostKeyChecking=accept-new",
                       "-o", f"UserKnownHostsFile={self.known_hosts_path}", "-o", "LogLevel=ERROR"]
            env = {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": str(self.state_dir)}
            if node["auth_type"] == "password":
                command += ["-o", "BatchMode=no", "-o", "PasswordAuthentication=yes", "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"]
                env.update({"DISPLAY": "relay:0", "SSH_ASKPASS_REQUIRE": "force", "SSH_ASKPASS": str(self.ensure_askpass_helper()), "RELAY_SSH_PASSWORD_FILE": node["password_path"]})
                if Path("/usr/bin/setsid").exists():
                    command = ["/usr/bin/setsid", "-w", *command]
            else:
                command += ["-i", node["key_path"], "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no"]
            command += [target, "printf relay-ok"]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False, env=env)
                online = result.returncode == 0 and result.stdout == "relay-ok"
                error = None if online else (result.stderr.strip() or "SSH 连接失败")[-500:]
            except subprocess.TimeoutExpired:
                online, error = False, "SSH 连接超时"
            except OSError as exc:
                online, error = False, f"无法启动 SSH：{exc}"
            with self.connect() as db:
                db.execute(
                    "UPDATE nodes SET status = ?, last_error = ?, last_seen_at = ? WHERE id = ?",
                    ("online" if online else "offline", error, iso_now() if online else None, node_id),
                )

    def setup_required(self) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            return bool(row and row["count"] == 0)

    def create_session(self, db: sqlite3.Connection, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = utc_now()
        expires = now + timedelta(days=7)
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now.isoformat(timespec="seconds"),))
        db.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, expires.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
        )
        return token

    def user_for_token(self, token: str | None) -> sqlite3.Row | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        now = iso_now()
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            return db.execute(
                """
                SELECT users.id, users.username, users.display_name, users.role
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()


def human_bytes(value: int | None) -> str:
    if value is None:
        return "等待扫描"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    amount = float(value)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    precision = 0 if amount >= 100 else 1
    return f"{amount:.{precision}f} {unit}"


def human_duration(seconds: int | None, fallback: str) -> str:
    if seconds is None:
        return fallback
    hours, remainder = divmod(max(seconds, 0), 3600)
    minutes, seconds = divmod(remainder, 60)
    if not hours and not minutes:
        return f"{seconds}s"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def task_to_json(row: sqlite3.Row) -> dict[str, Any]:
    status = row["status"]
    verification_status = row["verification_status"]
    eta_fallback = {
        "queued": "等待执行",
        "paused": "已暂停",
        "failed": row["error_message"] or "执行失败",
        "completed": "完成",
        "transferring": "计算中",
    }[status]
    if verification_status == "running":
        eta_fallback = "正在校验内容"
    keys = set(row.keys())
    def node_details(prefix: str) -> dict[str, Any] | None:
        host_key = f"{prefix}_node_host"
        if host_key not in keys or row[host_key] is None:
            return None
        return {
            "name": row[f"{prefix}_node_name"],
            "host": row[host_key],
            "ssh_port": row[f"{prefix}_node_port"],
            "transfer_host": row[f"{prefix}_node_transfer_host"],
            "transfer_port": row[f"{prefix}_node_transfer_port"],
        }
    return {
        "id": row["id"],
        "name": row["name"],
        "owner_id": row["owner_id"],
        "owner_username": row["owner_username"] if "owner_username" in keys else None,
        "owner_display_name": row["owner_display_name"] if "owner_display_name" in keys else None,
        "source": row["source"],
        "destination": row["destination"],
        "size": human_bytes(row["total_bytes"]),
        "transferred": human_bytes(row["transferred_bytes"]),
        "progress": row["progress"],
        "status": status,
        "speed": f"{human_bytes(row['speed_bps'])}/s" if row["speed_bps"] else "—",
        "eta": human_duration(row["eta_seconds"], eta_fallback),
        "files": f"{row['file_count']:,}" if row["file_count"] is not None else "等待扫描",
        "started": row["started_at"] or "—",
        "updated": row["updated_at"],
        "kind": row["kind"],
        "error": row["error_message"],
        "log": row["log_tail"] or "",
        "source_node_id": row["source_node_id"],
        "destination_node_id": row["destination_node_id"],
        "source_path": row["source_path"],
        "destination_path": row["destination_path"],
        "direct_host": row["direct_host"],
        "direct_port": row["direct_port"],
        "delete_enabled": bool(row["delete_enabled"]),
        "bandwidth_limit_kbps": row["bandwidth_limit_kbps"],
        "max_size_bytes": row["max_size_bytes"],
        "schedule_at": row["schedule_at"],
        "scheduled": bool(row["schedule_at"] and row["status"] == "queued" and row["schedule_at"] > iso_now()),
        "verify_after_transfer": bool(row["verify_after_transfer"]),
        "verification_status": verification_status,
        "verification_message": row["verification_message"],
        "source_node": node_details("source"),
        "destination_node": node_details("destination"),
    }


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: RelayApp):
        self.app = app
        super().__init__(address, RelayHandler)


class RelayHandler(BaseHTTPRequestHandler):
    server: RelayServer
    server_version = "relay-api"
    sys_version = ""

    def log_message(self, message: str, *args: Any) -> None:
        sys.stderr.write(f"{iso_now()} {self.address_string()} {message % args}\n")

    def _json(self, status: int, payload: dict[str, Any] | list[Any], *, cookie: str | None = None) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("请求长度不正确") from exc
        if length <= 0 or length > 65_536:
            raise ValueError("请求内容为空或过大")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("JSON 内容不正确") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求内容格式不正确")
        return payload

    def _session_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _current_user(self) -> sqlite3.Row | None:
        return self.server.app.user_for_token(self._session_token())

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        return forwarded.split(",", 1)[0].strip() or self.client_address[0]

    def _user_json(self, user: sqlite3.Row) -> dict[str, Any]:
        return {"id": user["id"], "username": user["username"], "display_name": user["display_name"], "role": user["role"]}

    @staticmethod
    def _is_admin(user: sqlite3.Row) -> bool:
        return user["role"] == "admin"

    def _require_admin(self, user: sqlite3.Row) -> bool:
        if not self._is_admin(user):
            self._json(HTTPStatus.FORBIDDEN, {"error": "只有管理员可以执行此操作"})
            return False
        return True

    def _auth_cookie(self, token: str) -> str:
        secure = "; Secure" if self.server.app.secure_cookie else ""
        return f"{SESSION_COOKIE}={token}; Path=/relay; HttpOnly; SameSite=Strict; Max-Age=604800{secure}"

    def _clear_cookie(self) -> str:
        secure = "; Secure" if self.server.app.secure_cookie else ""
        return f"{SESSION_COOKIE}=; Path=/relay; HttpOnly; SameSite=Strict; Max-Age=0{secure}"

    def _check_write_request(self) -> bool:
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != self.server.app.allowed_origin:
            self._json(HTTPStatus.FORBIDDEN, {"error": "请求来源不允许"})
            return False
        if self.headers.get("X-Relay-Request") != "1":
            self._json(HTTPStatus.FORBIDDEN, {"error": "请求校验失败"})
            return False
        return True

    def _require_user(self) -> sqlite3.Row | None:
        user = self._current_user()
        if not user:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "请先登录"})
            return None
        return user

    def do_GET(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == f"{API_PREFIX}/health":
            self._json(HTTPStatus.OK, {"ok": True, "setup_required": self.server.app.setup_required()})
            return
        if path == f"{API_PREFIX}/bootstrap":
            user = self._current_user()
            self._json(HTTPStatus.OK, {"setup_required": self.server.app.setup_required(), "user": self._user_json(user) if user else None})
            return

        user = self._require_user()
        if not user:
            return
        if path == f"{API_PREFIX}/tasks":
            with self.server.app.connect() as db:
                task_scope = "" if self._is_admin(user) else "WHERE tasks.owner_id = ?"
                params: tuple[Any, ...] = () if self._is_admin(user) else (user["id"],)
                rows = db.execute(
                    f"""
                    SELECT tasks.*,
                           users.username AS owner_username, users.display_name AS owner_display_name,
                           source.name AS source_node_name, source.host AS source_node_host,
                           source.ssh_port AS source_node_port, source.transfer_host AS source_node_transfer_host,
                           source.transfer_port AS source_node_transfer_port,
                           destination.name AS destination_node_name, destination.host AS destination_node_host,
                           destination.ssh_port AS destination_node_port, destination.transfer_host AS destination_node_transfer_host,
                           destination.transfer_port AS destination_node_transfer_port
                    FROM tasks
                    LEFT JOIN nodes AS source ON source.id = tasks.source_node_id
                    LEFT JOIN nodes AS destination ON destination.id = tasks.destination_node_id
                    JOIN users ON users.id = tasks.owner_id
                    {task_scope}
                    ORDER BY tasks.updated_at DESC
                    """,
                    params,
                ).fetchall()
            self._json(HTTPStatus.OK, {"tasks": [task_to_json(row) for row in rows]})
            return
        if path == f"{API_PREFIX}/transfer-settings":
            self._json(HTTPStatus.OK, self.server.app.transfer_settings())
            return
        if path == f"{API_PREFIX}/nodes":
            with self.server.app.connect() as db:
                rows = db.execute(
                    """
                    SELECT nodes.id, nodes.name, nodes.status, nodes.host, nodes.ssh_port, nodes.username,
                           nodes.transfer_host, nodes.transfer_port,
                           nodes.last_error, nodes.last_seen_at, nodes.created_at,
                           credentials.name AS credential_name, credentials.fingerprint,
                           credentials.auth_type
                    FROM nodes JOIN credentials ON credentials.id = nodes.credential_id
                    ORDER BY nodes.created_at DESC
                """,
                ).fetchall()
            self._json(HTTPStatus.OK, {"nodes": [dict(row) for row in rows]})
            return
        if path == f"{API_PREFIX}/users":
            if not self._require_admin(user):
                return
            with self.server.app.connect() as db:
                rows = db.execute("SELECT id, username, display_name, role, created_at FROM users ORDER BY id").fetchall()
            self._json(HTTPStatus.OK, {"users": [dict(row) for row in rows]})
            return
        if path == f"{API_PREFIX}/activity":
            limit = 100
            try:
                requested = int(urlsplit(self.path).query.split("limit=", 1)[1].split("&", 1)[0]) if "limit=" in urlsplit(self.path).query else 100
                limit = max(1, min(requested, 200))
            except (TypeError, ValueError):
                pass
            actor_filter = "" if self._is_admin(user) else "WHERE audit_events.actor_id = ?"
            params = (limit,) if self._is_admin(user) else (user["id"], limit)
            with self.server.app.connect() as db:
                rows = db.execute(
                    f"""
                    SELECT audit_events.id, audit_events.action, audit_events.entity_type, audit_events.entity_id,
                           audit_events.detail, audit_events.created_at,
                           users.username AS actor_username, users.display_name AS actor_display_name
                    FROM audit_events LEFT JOIN users ON users.id = audit_events.actor_id
                    {actor_filter} ORDER BY audit_events.created_at DESC, audit_events.id DESC LIMIT ?
                    """, params,
                ).fetchall()
            self._json(HTTPStatus.OK, {"events": [dict(row) for row in rows]})
            return
        if path == f"{API_PREFIX}/node-setup":
            if not self._require_admin(user):
                return
            try:
                credential, public_key = self.server.app.default_node_credential(user["id"])
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            command = f"echo {shlex.quote(public_key)} >> ~/.ssh/authorized_keys"
            self._json(HTTPStatus.OK, {
                "credential_name": credential["name"],
                "fingerprint": credential["fingerprint"],
                "public_key": public_key,
                "authorized_keys_command": command,
            })
            return
        if path == f"{API_PREFIX}/me":
            self._json(HTTPStatus.OK, {"user": self._user_json(user)})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        if not self._check_write_request():
            return
        try:
            if path == f"{API_PREFIX}/setup":
                self._handle_setup()
                return
            if path == f"{API_PREFIX}/login":
                self._handle_login()
                return
            if path == f"{API_PREFIX}/logout":
                self._handle_logout()
                return
            if path == f"{API_PREFIX}/change-password":
                user = self._require_user()
                if user:
                    self._handle_change_password(user)
                return

            user = self._require_user()
            if not user:
                return
            if path == f"{API_PREFIX}/tasks":
                self._handle_create_task(user)
                return
            if path == f"{API_PREFIX}/transfer-settings":
                if not self._require_admin(user):
                    return
                self._handle_transfer_settings(user)
                return
            if path == f"{API_PREFIX}/nodes/bulk":
                if not self._require_admin(user):
                    return
                self._handle_create_nodes(user)
                return
            if path == f"{API_PREFIX}/users":
                if not self._require_admin(user):
                    return
                self._handle_create_user(user)
                return
            user_action = USER_ACTION_PATTERN.match(path)
            if user_action:
                if not self._require_admin(user):
                    return
                self._handle_reset_password(user, int(user_action.group(1)))
                return
            match = TASK_ACTION_PATTERN.match(path)
            if match:
                self._handle_task_action(user, match.group(1), match.group(2))
                return
            node_match = NODE_ACTION_PATTERN.match(path)
            if node_match:
                if not self._require_admin(user):
                    return
                if node_match.group(2) == "test":
                    self._handle_test_node(user, node_match.group(1))
                else:
                    self._handle_transfer_route(user, node_match.group(1))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except sqlite3.IntegrityError:
            self._json(HTTPStatus.CONFLICT, {"error": "数据已存在或状态冲突"})

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        if not self._check_write_request():
            return
        user = self._require_user()
        if not user:
            return
        task_match = TASK_PATTERN.match(path)
        if task_match:
            try:
                self._handle_delete_task(user, task_match.group(1))
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        match = NODE_PATTERN.match(path)
        if not match:
            self._json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        try:
            if not self._require_admin(user):
                return
            self._handle_delete_node(user, match.group(1))
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except sqlite3.IntegrityError:
            self._json(HTTPStatus.CONFLICT, {"error": "数据状态冲突"})

    def _handle_setup(self) -> None:
        payload = self._read_json()
        username = clean_text(payload.get("username"), field="用户名", maximum=32)
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError("用户名只能使用字母、数字、点、横线和下划线")
        display_name = clean_text(payload.get("display_name"), field="显示名称", maximum=60)
        password = clean_text(payload.get("password"), field="密码", maximum=128)
        if len(password) < 12:
            raise ValueError("密码至少需要 12 个字符")
        salt, digest = hash_password(password)
        with self.server.app.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise ValueError("管理员已经创建，请直接登录")
            cursor = db.execute(
                "INSERT INTO users(username, display_name, password_salt, password_hash, role, created_at) VALUES (?, ?, ?, ?, 'admin', ?)",
                (username, display_name, salt, digest, iso_now()),
            )
            token = self.server.app.create_session(db, cursor.lastrowid)
            user = db.execute("SELECT id, username, display_name, role FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        self.server.app.audit(user["id"], "setup", "user", user["id"], "初始化管理员")
        self._json(HTTPStatus.CREATED, {"user": self._user_json(user)}, cookie=self._auth_cookie(token))

    def _handle_login(self) -> None:
        client_ip = self._client_ip()
        if not self.server.app.login_allowed(client_ip):
            raise ValueError("登录尝试次数过多，请 15 分钟后重试")
        payload = self._read_json()
        username = clean_text(payload.get("username"), field="用户名", maximum=32)
        password = clean_text(payload.get("password"), field="密码", maximum=128)
        with self.server.app.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if not row:
                self.server.app.record_login_failure(client_ip)
                raise ValueError("用户名或密码不正确")
            _, digest = hash_password(password, row["password_salt"])
            if not hmac.compare_digest(digest, row["password_hash"]):
                self.server.app.record_login_failure(client_ip)
                raise ValueError("用户名或密码不正确")
            token = self.server.app.create_session(db, row["id"])
        self.server.app.clear_login_failures(client_ip)
        self.server.app.audit(row["id"], "login", "session", detail="登录成功")
        self._json(HTTPStatus.OK, {"user": self._user_json(row)}, cookie=self._auth_cookie(token))

    def _handle_logout(self) -> None:
        user = self._current_user()
        token = self._session_token()
        if token:
            token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
            with self.server.app.connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        if user:
            self.server.app.audit(user["id"], "logout", "session", detail="退出登录")
        self._json(HTTPStatus.OK, {"ok": True}, cookie=self._clear_cookie())

    def _handle_change_password(self, user: sqlite3.Row) -> None:
        payload = self._read_json()
        current = clean_text(payload.get("current_password"), field="当前密码", maximum=128)
        new_password = clean_text(payload.get("new_password"), field="新密码", maximum=128)
        if len(new_password) < 12:
            raise ValueError("新密码至少需要 12 个字符")
        with self.server.app.connect() as db:
            row = db.execute("SELECT password_salt, password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
            _, digest = hash_password(current, row["password_salt"])
            if not hmac.compare_digest(digest, row["password_hash"]):
                raise ValueError("当前密码不正确")
            salt, password_hash = hash_password(new_password)
            db.execute("UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?", (salt, password_hash, user["id"]))
        self.server.app.audit(user["id"], "change_password", "user", user["id"], "用户修改自己的密码")
        self._json(HTTPStatus.OK, {"ok": True})

    def _handle_create_user(self, actor: sqlite3.Row) -> None:
        payload = self._read_json()
        username = clean_text(payload.get("username"), field="用户名", maximum=32)
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError("用户名只能使用字母、数字、点、横线和下划线")
        display_name = clean_text(payload.get("display_name"), field="显示名称", maximum=60)
        password = clean_text(payload.get("password"), field="密码", maximum=128)
        if len(password) < 12:
            raise ValueError("密码至少需要 12 个字符")
        role = payload.get("role", "user")
        if role not in ("admin", "user"):
            raise ValueError("账号角色不正确")
        salt, password_hash = hash_password(password)
        with self.server.app.connect() as db:
            cursor = db.execute(
                "INSERT INTO users(username, display_name, password_salt, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (username, display_name, salt, password_hash, role, iso_now()),
            )
            created = db.execute("SELECT id, username, display_name, role, created_at FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        self.server.app.audit(actor["id"], "create_user", "user", created["id"], f"创建账号 {created['username']}（{role}）")
        self._json(HTTPStatus.CREATED, {"user": dict(created)})

    def _handle_reset_password(self, actor: sqlite3.Row, user_id: int) -> None:
        payload = self._read_json()
        password = clean_text(payload.get("password"), field="新密码", maximum=128)
        if len(password) < 12:
            raise ValueError("新密码至少需要 12 个字符")
        salt, password_hash = hash_password(password)
        with self.server.app.connect() as db:
            target = db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
            if not target:
                raise ValueError("账号不存在")
            db.execute("UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?", (salt, password_hash, user_id))
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self.server.app.audit(actor["id"], "reset_password", "user", user_id, f"管理员重置账号 {target['username']} 的密码")
        self._json(HTTPStatus.OK, {"ok": True})

    def _handle_create_task(self, user: sqlite3.Row) -> None:
        payload = self._read_json()
        name = clean_text(payload.get("name", "新传输任务"), field="任务名称", maximum=120, required=False) or "新传输任务"
        source_node_id = clean_text(payload.get("source_node_id"), field="源节点", maximum=36)
        destination_node_id = clean_text(payload.get("destination_node_id"), field="目标节点", maximum=36)
        if source_node_id == destination_node_id:
            raise ValueError("源节点和目标节点不能相同")
        source_path = clean_path(payload.get("source_path"), field="源路径")
        destination_path = clean_path(payload.get("destination_path"), field="目标路径")
        direct_host_value = payload.get("direct_host", "")
        if not isinstance(direct_host_value, str):
            raise ValueError("本次直传地址格式不正确")
        direct_host = self.server.app.validate_host(direct_host_value.strip()) if direct_host_value.strip() else None
        direct_port_value = payload.get("direct_port")
        if direct_host is None and direct_port_value not in (None, ""):
            raise ValueError("填写直传端口时必须同时填写直传地址")
        delete_enabled = payload.get("delete_enabled", False)
        if not isinstance(delete_enabled, bool):
            raise ValueError("删除目标多余文件选项格式不正确")
        verify_after_transfer = payload.get("verify_after_transfer", False)
        if not isinstance(verify_after_transfer, bool):
            raise ValueError("内容校验选项格式不正确")
        bandwidth_value = payload.get("bandwidth_limit_mbps", 0)
        try:
            bandwidth_mbps = int(bandwidth_value or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("限速格式不正确") from exc
        if not 0 <= bandwidth_mbps <= 10_240:
            raise ValueError("限速必须在 0 到 10240 MB/s 之间")
        bandwidth_kbps = bandwidth_mbps * 1024 or None
        size_limit_value = payload.get("max_size_gb", 100)
        if isinstance(size_limit_value, bool):
            raise ValueError("任务大小上限格式不正确")
        try:
            size_limit_gb = int(size_limit_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("任务大小上限格式不正确") from exc
        if not 0 <= size_limit_gb <= MAX_TASK_SIZE_LIMIT_GB:
            raise ValueError(f"任务大小上限必须在 0 到 {MAX_TASK_SIZE_LIMIT_GB} GB 之间")
        # Keep zero in the database as the explicit "unlimited" value.  The
        # column is intentionally NOT NULL so that historical tasks remain
        # unambiguous and queryable.
        max_size_bytes = size_limit_gb * 1024 * 1024 * 1024
        schedule_at = clean_schedule_at(payload.get("schedule_at"))
        task_id = str(uuid.uuid4())
        now = iso_now()
        with self.server.app.connect() as db:
            nodes = db.execute(
                """
                SELECT nodes.id, nodes.name, nodes.status, nodes.host, nodes.ssh_port, nodes.username,
                       credentials.key_path, credentials.auth_type, credentials.password_path
                FROM nodes JOIN credentials ON credentials.id = nodes.credential_id
                WHERE nodes.id IN (?, ?)
                """,
                (source_node_id, destination_node_id),
            ).fetchall()
            by_id = {node["id"]: node for node in nodes}
            if source_node_id not in by_id or destination_node_id not in by_id:
                raise ValueError("源节点或目标节点不存在")
            if by_id[source_node_id]["status"] != "online" or by_id[destination_node_id]["status"] != "online":
                raise ValueError("源节点和目标节点必须在线")
        if direct_host is None:
            direct_port = None
        elif direct_port_value in (None, ""):
            direct_port = by_id[destination_node_id]["ssh_port"]
        else:
            try:
                direct_port = int(direct_port_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("本次直传端口格式不正确") from exc
            if not 1 <= direct_port <= 65535:
                raise ValueError("本次直传端口必须在 1 到 65535 之间")
        try:
            total_bytes, source_is_directory = self.server.app.transfers.preflight(
                by_id[source_node_id], source_path, by_id[destination_node_id], destination_path,
            )
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        if delete_enabled and not source_is_directory:
            raise ValueError("镜像删除仅支持目录源路径")
        if max_size_bytes and total_bytes > max_size_bytes:
            raise ValueError(
                f"源数据总大小 {human_bytes(total_bytes)} 超过任务上限 {human_bytes(max_size_bytes)}"
            )
        source = f"{by_id[source_node_id]['name']}:{source_path}"
        destination = f"{by_id[destination_node_id]['name']}:{destination_path}"
        with self.server.app.connect() as db:
            db.execute(
                """
                INSERT INTO tasks(
                    id, name, source, destination, status, progress, created_at, updated_at, owner_id,
                    source_node_id, destination_node_id, source_path, destination_path,
                    delete_enabled, bandwidth_limit_kbps, max_size_bytes, total_bytes, schedule_at,
                    direct_host, direct_port, verify_after_transfer
                )
                VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, name, source, destination, now, now, user["id"], source_node_id,
                    destination_node_id, source_path, destination_path, int(delete_enabled), bandwidth_kbps,
                    max_size_bytes, total_bytes, schedule_at, direct_host, direct_port, int(verify_after_transfer),
                ),
            )
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not schedule_at and not self.server.app.transfers.start(task_id):
            raise ValueError("任务已经在执行")
        self.server.app.audit(user["id"], "create_task", "task", task_id, f"创建传输任务 {name}")
        self._json(HTTPStatus.CREATED, {"task": task_to_json(row)})

    def _handle_transfer_settings(self, user: sqlite3.Row) -> None:
        payload = self._read_json()
        values: dict[str, int] = {}
        for field, maximum, label in (
            ("max_concurrent", MAX_CONCURRENT_TRANSFERS, "全局并发"),
            ("max_per_node", MAX_TRANSFERS_PER_NODE, "单节点并发"),
        ):
            value = payload.get(field)
            if isinstance(value, bool):
                raise ValueError(f"{label}格式不正确")
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label}格式不正确") from exc
            if not 1 <= value <= maximum:
                raise ValueError(f"{label}必须在 1 到 {maximum} 之间")
            values[field] = value
        with self.server.app.connect() as db:
            db.execute(
                "UPDATE transfer_settings SET max_concurrent = ?, max_per_node = ?, updated_at = ? WHERE id = 1",
                (values["max_concurrent"], values["max_per_node"], iso_now()),
            )
        self.server.app.transfers.configure_limits(values["max_concurrent"], values["max_per_node"])
        self.server.app.audit(user["id"], "update_transfer_settings", "settings", detail=f"全局并发 {values['max_concurrent']}，单节点并发 {values['max_per_node']}")
        self._json(HTTPStatus.OK, values)

    def _handle_task_action(self, user: sqlite3.Row, task_id: str, action: str) -> None:
        with self.server.app.connect() as db:
            scope = "" if self._is_admin(user) else " AND owner_id = ?"
            params: tuple[Any, ...] = (task_id,) if self._is_admin(user) else (task_id, user["id"])
            row = db.execute(f"SELECT * FROM tasks WHERE id = ?{scope}", params).fetchone()
            if not row:
                raise ValueError("任务不存在")
            if action == "pause":
                if row["status"] not in ("queued", "transferring"):
                    raise ValueError("当前任务不能暂停")
                self.server.app.transfers.cancel(task_id)
                db.execute(
                    "UPDATE tasks SET status = 'paused', progress_base_bytes = transferred_bytes, speed_bps = 0, eta_seconds = NULL, error_message = NULL, updated_at = ? WHERE id = ?",
                    (iso_now(), task_id),
                )
            else:
                allowed = ("paused",) if action == "resume" else ("failed", "completed")
                if row["status"] not in allowed:
                    raise ValueError("当前任务不能重新执行")
                if not row["source_node_id"] or not row["destination_node_id"]:
                    raise ValueError("旧任务没有节点配置，请重新创建")
                db.execute(
                    "UPDATE tasks SET status = 'queued', error_message = NULL, speed_bps = NULL, eta_seconds = NULL, completed_at = NULL, verification_status = NULL, verification_message = NULL, schedule_at = CASE WHEN ? = 'retry' THEN NULL ELSE schedule_at END, progress_base_bytes = CASE WHEN ? = 'retry' THEN 0 ELSE progress_base_bytes END, transferred_bytes = CASE WHEN ? = 'retry' THEN 0 ELSE transferred_bytes END, progress = CASE WHEN ? = 'retry' THEN 0 ELSE progress END, updated_at = ? WHERE id = ?",
                    (action, action, action, action, iso_now(), task_id),
                )
            updated = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        scheduled_for_later = bool(updated["schedule_at"] and updated["schedule_at"] > iso_now())
        if action != "pause" and not scheduled_for_later and not self.server.app.transfers.start(task_id):
            raise ValueError("任务正在停止，请稍后重试")
        self.server.app.audit(user["id"], action, "task", task_id, f"对任务执行 {action}")
        self._json(HTTPStatus.OK, {"task": task_to_json(updated)})

    def _handle_create_nodes(self, user: sqlite3.Row) -> None:
        payload = self._read_json()
        hosts_value = payload.get("hosts")
        if isinstance(hosts_value, str):
            raw_hosts = [line.strip() for line in hosts_value.splitlines() if line.strip()]
        elif isinstance(hosts_value, list):
            raw_hosts = hosts_value
        else:
            raise ValueError("请填写节点地址")
        transfer_hosts_value = payload.get("transfer_hosts", "")
        if not isinstance(transfer_hosts_value, str):
            raise ValueError("直传地址格式不正确")
        raw_transfer_hosts = transfer_hosts_value.splitlines()
        if raw_transfer_hosts and len(raw_transfer_hosts) != len(raw_hosts):
            raise ValueError("直传地址需与节点地址逐行对应")
        host_routes: list[tuple[str, str | None]] = []
        for index, value in enumerate(raw_hosts):
            host = self.server.app.validate_host(value)
            transfer_value = raw_transfer_hosts[index].strip() if raw_transfer_hosts else ""
            transfer_host = self.server.app.validate_host(transfer_value) if transfer_value else None
            if host not in {item[0] for item in host_routes}:
                host_routes.append((host, transfer_host))
        hosts = [item[0] for item in host_routes]
        if not hosts or len(hosts) > 20:
            raise ValueError("每次可添加 1 到 20 台节点")

        username = clean_text(payload.get("username"), field="SSH 用户", maximum=32)
        if not SSH_USERNAME_PATTERN.fullmatch(username):
            raise ValueError("SSH 用户名格式不正确")
        try:
            ssh_port = int(payload.get("ssh_port"))
        except (TypeError, ValueError) as exc:
            raise ValueError("SSH 端口格式不正确") from exc
        if not 1 <= ssh_port <= 65535:
            raise ValueError("SSH 端口必须在 1 到 65535 之间")
        transfer_port_value = payload.get("transfer_ssh_port")
        if transfer_port_value in (None, ""):
            transfer_port = ssh_port
        else:
            try:
                transfer_port = int(transfer_port_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("直传端口格式不正确") from exc
            if not 1 <= transfer_port <= 65535:
                raise ValueError("直传端口必须在 1 到 65535 之间")
        name_prefix = clean_text(payload.get("name_prefix", "node"), field="节点名称", maximum=48, required=False) or "node"
        credential_name = clean_text(payload.get("credential_name", "SSH key"), field="私钥名称", maximum=80, required=False) or "SSH key"
        auth_type = payload.get("auth_type", "key")
        if auth_type not in ("key", "password"):
            raise ValueError("SSH 认证方式不正确")
        key_path: str | None = None
        password_path: str | None = None
        if auth_type == "password":
            credential_id, password_path = self.server.app.store_password(payload.get("ssh_password"))
            # Keep key_path populated for installations created with the original
            # NOT NULL schema; password authentication never reads it as a key.
            key_path = password_path
            fingerprint = "密码认证"
        else:
            credential_id, key_path, fingerprint = self.server.app.store_private_key(payload.get("private_key"))
        now = iso_now()
        node_ids: list[str] = []
        try:
            with self.server.app.connect() as db:
                db.execute(
                    "INSERT INTO credentials(id, name, fingerprint, key_path, created_at, owner_id, auth_type, password_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (credential_id, credential_name, fingerprint, key_path, now, user["id"], auth_type, password_path),
                )
                for index, (host, transfer_host) in enumerate(host_routes, 1):
                    node_id = str(uuid.uuid4())
                    node_name = name_prefix if len(hosts) == 1 else f"{name_prefix}-{index:02d}"
                    db.execute(
                        """
                        INSERT INTO nodes(id, name, status, host, ssh_port, username, transfer_host, transfer_port, credential_id, created_at)
                        VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (node_id, node_name, host, ssh_port, username, transfer_host, transfer_port if transfer_host else None, credential_id, now),
                    )
                    node_ids.append(node_id)
        except Exception:
            if key_path:
                Path(key_path).unlink(missing_ok=True)
            if password_path:
                Path(password_path).unlink(missing_ok=True)
            raise
        self.server.app.test_node_async(node_ids)
        self.server.app.audit(user["id"], "create_node", "node", detail=f"添加 {len(node_ids)} 台节点（{auth_type}）")
        self._json(HTTPStatus.CREATED, {"created": len(node_ids), "node_ids": node_ids, "fingerprint": fingerprint})

    def _handle_test_node(self, user: sqlite3.Row, node_id: str) -> None:
        with self.server.app.connect() as db:
            node = db.execute(
                """
                SELECT nodes.id FROM nodes
                JOIN credentials ON credentials.id = nodes.credential_id
                WHERE nodes.id = ?
                """,
                (node_id,),
            ).fetchone()
            if not node:
                raise ValueError("节点不存在")
            db.execute("UPDATE nodes SET status = 'pending', last_error = NULL WHERE id = ?", (node_id,))
        self.server.app.test_node_async([node_id])
        self.server.app.audit(user["id"], "test_node", "node", node_id, "重新测试节点连接")
        self._json(HTTPStatus.ACCEPTED, {"ok": True})

    def _handle_transfer_route(self, user: sqlite3.Row, node_id: str) -> None:
        payload = self._read_json()
        host_value = payload.get("transfer_host", "")
        if not isinstance(host_value, str):
            raise ValueError("直传地址格式不正确")
        transfer_host = self.server.app.validate_host(host_value.strip()) if host_value.strip() else None
        with self.server.app.connect() as db:
            node = db.execute(
                """
                SELECT nodes.id, nodes.ssh_port FROM nodes
                JOIN credentials ON credentials.id = nodes.credential_id
                WHERE nodes.id = ?
                """,
                (node_id,),
            ).fetchone()
            if not node:
                raise ValueError("节点不存在")
            port_value = payload.get("transfer_port")
            if transfer_host is None:
                transfer_port = None
            elif port_value in (None, ""):
                transfer_port = node["ssh_port"]
            else:
                try:
                    transfer_port = int(port_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("直传端口格式不正确") from exc
                if not 1 <= transfer_port <= 65535:
                    raise ValueError("直传端口必须在 1 到 65535 之间")
            db.execute(
                "UPDATE nodes SET transfer_host = ?, transfer_port = ? WHERE id = ?",
                (transfer_host, transfer_port, node_id),
            )
            updated = db.execute(
                "SELECT id, transfer_host, transfer_port FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
        self._json(HTTPStatus.OK, {"node": dict(updated)})

    def _handle_delete_task(self, user: sqlite3.Row, task_id: str) -> None:
        with self.server.app.connect() as db:
            scope = "" if self._is_admin(user) else " AND owner_id = ?"
            params: tuple[Any, ...] = (task_id,) if self._is_admin(user) else (task_id, user["id"])
            task = db.execute(f"SELECT id, name, status, schedule_at FROM tasks WHERE id = ?{scope}", params).fetchone()
            if not task:
                raise ValueError("任务不存在")
            if task["status"] == "transferring":
                raise ValueError("任务正在传输，请先暂停后再删除")
            is_scheduled = bool(task["schedule_at"] and task["schedule_at"] > iso_now())
            if task["status"] == "queued" and not is_scheduled:
                raise ValueError("任务正在启动，请先暂停后再删除")
            if task["status"] == "paused" and self.server.app.transfers.is_running(task_id):
                raise ValueError("暂停正在生效，请稍候再删除")
            db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.server.app.audit(user["id"], "delete_task", "task", task_id, f"删除传输任务 {task['name']}")
        self._json(HTTPStatus.OK, {"ok": True, "deleted": task["name"]})

    def _handle_delete_node(self, user: sqlite3.Row, node_id: str) -> None:
        key_path: str | None = None
        with self.server.app.connect() as db:
            node = db.execute(
                """
                SELECT nodes.id, nodes.name, nodes.credential_id, credentials.key_path, credentials.password_path
                FROM nodes JOIN credentials ON credentials.id = nodes.credential_id
                WHERE nodes.id = ?
                """,
                (node_id,),
            ).fetchone()
            if not node:
                raise ValueError("节点不存在")
            active_task = db.execute(
                """
                SELECT 1 FROM tasks
                WHERE status IN ('queued', 'transferring')
                  AND (source_node_id = ? OR destination_node_id = ?)
                LIMIT 1
                """,
                (node_id, node_id),
            ).fetchone()
            if active_task:
                raise ValueError("该节点有正在执行或等待执行的任务，不能删除")
            db.execute("UPDATE tasks SET source_node_id = NULL WHERE source_node_id = ?", (node_id,))
            db.execute("UPDATE tasks SET destination_node_id = NULL WHERE destination_node_id = ?", (node_id,))
            db.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            remaining = db.execute(
                "SELECT COUNT(*) AS count FROM nodes WHERE credential_id = ?", (node["credential_id"],)
            ).fetchone()
            if remaining and remaining["count"] == 0:
                db.execute("DELETE FROM credentials WHERE id = ?", (node["credential_id"],))
                key_path = node["key_path"]
                password_path = node["password_path"]
            else:
                password_path = None
        if key_path:
            Path(key_path).unlink(missing_ok=True)
        if password_path:
            Path(password_path).unlink(missing_ok=True)
        self.server.app.audit(user["id"], "delete_node", "node", node_id, f"删除节点 {node['name']}")
        self._json(HTTPStatus.OK, {"ok": True, "deleted": node["name"]})


def main() -> None:
    db_path = os.environ.get("RELAY_DB_PATH", "/var/lib/relay-api/relay.db")
    allowed_origin = os.environ.get("RELAY_ALLOWED_ORIGIN", "http://localhost:8080")
    host = os.environ.get("RELAY_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("RELAY_BIND_PORT", "18777"))
    secure_cookie = os.environ.get("RELAY_SECURE_COOKIE", "1") != "0"
    app = RelayApp(db_path, allowed_origin, secure_cookie)
    server = RelayServer((host, port), app)
    stopping = threading.Event()

    def stop_server(signum, frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(f"relay-api listening on http://{host}:{port}", flush=True)
    server.timeout = 0.5
    try:
        # Avoid a serve_forever()/shutdown() startup race when a container is
        # stopped immediately after spawning the API. Requests remain threaded.
        while not stopping.is_set():
            server.handle_request()
    finally:
        if not app.transfers.shutdown():
            print("Transfer cleanup timed out; unfinished tasks can be retried after restart", file=sys.stderr)
        server.server_close()
        print("relay-api stopped", flush=True)


if __name__ == "__main__":
    main()
