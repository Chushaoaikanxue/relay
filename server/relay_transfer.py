#!/usr/bin/env python3
"""Background node-to-node rsync execution for Relay."""

from __future__ import annotations

import base64
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROGRESS_PATTERN = re.compile(
    r"^\s*([\d,]+)\s+(\d+)%\s+([\d.]+)([kMGTPE]?B)/s\s+(\d+):(\d+):(\d+)"
)
FILE_COUNT_PATTERN = re.compile(r"(?:to-chk|ir-chk)=(\d+)/(\d+)")
SPEED_MULTIPLIERS = {
    "B": 1,
    "kB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "PB": 1_000_000_000_000_000,
    "EB": 1_000_000_000_000_000_000,
}
RESTRICTED_RSYNC_WRAPPER = b"""#!/bin/sh
set -eu
exec /usr/bin/sudo -n /usr/bin/env "SSH_ORIGINAL_COMMAND=${SSH_ORIGINAL_COMMAND:-}" /usr/bin/rrsync "$@"
"""


class AdjustableSemaphore:
    """A cancellable semaphore whose limit can be changed while Relay is running."""

    def __init__(self, limit: int):
        self.limit = limit
        self.in_use = 0
        self.condition = threading.Condition()

    def acquire(self, cancel: threading.Event) -> bool:
        with self.condition:
            while not cancel.is_set():
                if self.in_use < self.limit:
                    self.in_use += 1
                    return True
                self.condition.wait(timeout=0.25)
        return False

    def release(self) -> None:
        with self.condition:
            if self.in_use:
                self.in_use -= 1
            self.condition.notify_all()

    def set_limit(self, limit: int) -> None:
        with self.condition:
            self.limit = limit
            self.condition.notify_all()


def parse_progress_line(line: str) -> dict[str, int] | None:
    """Parse an rsync --info=progress2 status line."""
    match = PROGRESS_PATTERN.search(line)
    if not match:
        return None
    transferred = int(match.group(1).replace(",", ""))
    progress = max(0, min(100, int(match.group(2))))
    speed = int(float(match.group(3)) * SPEED_MULTIPLIERS.get(match.group(4), 1))
    eta = int(match.group(5)) * 3600 + int(match.group(6)) * 60 + int(match.group(7))
    total = transferred if progress == 100 else (transferred * 100 // progress if progress else 0)
    result = {
        "transferred_bytes": transferred,
        "total_bytes": total,
        "progress": progress,
        "speed_bps": speed,
        "eta_seconds": eta,
    }
    files = FILE_COUNT_PATTERN.search(line)
    if files:
        result["file_count"] = int(files.group(2))
    return result


def overall_progress(total_bytes: int, rsync_progress: dict[str, int], base_bytes: int = 0) -> dict[str, int]:
    """Keep UI progress anchored to the size measured before a transfer starts.

    rsync progress2 can report a temporary total while it is still discovering
    files.  That number is useful for its own output, but not as a task total.
    """
    base = min(max(0, base_bytes), total_bytes)
    transferred = min(base + max(0, rsync_progress["transferred_bytes"]), total_bytes)
    speed = rsync_progress["speed_bps"]
    remaining = max(0, total_bytes - transferred)
    result = {
        "transferred_bytes": transferred,
        "total_bytes": total_bytes,
        # A running job is completed only after rsync exits successfully.
        "progress": min(99, transferred * 100 // total_bytes) if total_bytes else 0,
        "speed_bps": speed,
        "eta_seconds": remaining // speed if speed > 0 else 0,
    }
    if "file_count" in rsync_progress:
        result["file_count"] = rsync_progress["file_count"]
    return result


def _host_alias(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _known_host_lines(path: Path, host: str, port: int) -> str:
    alias = _host_alias(host, port)
    if not path.exists():
        raise RuntimeError("节点主机指纹尚未记录，请先重新测试节点")
    matches: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        host_field_index = 1 if fields[0].startswith("@") and len(fields) > 1 else 0
        if alias in fields[host_field_index].split(","):
            matches.append(stripped)
    if not matches:
        raise RuntimeError("目标节点主机指纹尚未记录，请先重新测试节点")
    return "\n".join(matches) + "\n"


def _retarget_known_host_lines(lines: str, host: str, port: int) -> str:
    """Bind an already-verified host key to the endpoint used between nodes."""
    alias = _host_alias(host, port)
    rewritten: list[str] = []
    for line in lines.splitlines():
        fields = line.split()
        index = 1 if fields and fields[0].startswith("@") else 0
        if len(fields) < index + 3 or fields[index].startswith("|"):
            raise RuntimeError("目标节点主机指纹格式不支持内网直传")
        fields[index] = alias
        rewritten.append(" ".join(fields))
    return "\n".join(rewritten) + "\n"


def format_data_size(value: int) -> str:
    amount = float(max(value, 0))
    unit = "B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if amount < 1024 or unit == "PB":
            break
        amount /= 1024
    return f"{amount:.1f} {unit}" if amount < 100 else f"{amount:.0f} {unit}"


def build_remote_command(
    task: sqlite3.Row, destination: sqlite3.Row, known_hosts: str, private_key_length: int, *, verify: bool = False,
) -> str:
    """Build the fixed, shell-quoted command executed on the source node."""
    task_id = task["id"]
    known_path = f"/tmp/.relay-known-{task_id}"
    key_path = f"/tmp/.relay-key-{task_id}"
    encoded_hosts = base64.b64encode(known_hosts.encode("utf-8")).decode("ascii")
    inner_ssh = shlex.join([
        "/usr/bin/ssh", "-F", "/dev/null", "-i", key_path, "-p", str(destination["destination_transfer_port"]),
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=2",
        "-o", "IdentitiesOnly=yes",
        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_path}",
        "-o", "LogLevel=ERROR",
    ])
    rsync_args = ["/usr/bin/rsync", "-rcln", "--out-format=%i %n%L"] if verify else [
        "/usr/bin/rsync", "-av", "--partial", "--partial-dir=.relay-partial",
        "--info=progress2", "--outbuf=L",
    ]
    if task["delete_enabled"] and not verify:
        rsync_args.append("--delete")
    if task["bandwidth_limit_kbps"] and not verify:
        rsync_args.append(f"--bwlimit={task['bandwidth_limit_kbps']}")
    rsync_args.extend([
        "-e", inner_ssh, "--", task["source_path"],
        f"{destination['destination_user']}@{destination['destination_transfer_host']}:/",
    ])
    quoted_rsync = " ".join(shlex.quote(argument) for argument in rsync_args)
    return (
        "set -eu; "
        f"known={shlex.quote(known_path)}; "
        f"key={shlex.quote(key_path)}; "
        "trap 'rm -f \"$known\" \"$key\"' EXIT HUP INT TERM; "
        "umask 077; "
        f"/usr/bin/head -c {private_key_length} > \"$key\"; "
        f"test \"$(/usr/bin/wc -c < \"$key\")\" -eq {private_key_length} || "
        "{ printf '%s\\n' '临时传输密钥接收失败'; exit 65; }; "
        f"printf %s {shlex.quote(encoded_hosts)} | /usr/bin/base64 -d > \"$known\"; "
        "chmod 600 \"$known\"; "
        f"sudo -n /usr/bin/test -e {shlex.quote(task['source_path'])} || "
        "{ printf '%s\\n' '源路径不存在或无权访问'; exit 66; }; "
        "sudo -n /usr/bin/env LC_ALL=C "
        f"{quoted_rsync}"
    )


class TransferManager:
    def __init__(
        self, db_path: str, state_dir: Path, known_hosts_path: Path, *, enabled: bool = True,
        max_concurrent: int = 4, max_per_node: int = 2,
    ):
        self.db_path = db_path
        self.state_dir = state_dir
        self.known_hosts_path = known_hosts_path
        self.askpass_path = state_dir / "keys" / "relay-ssh-askpass"
        if not self.askpass_path.exists():
            self.askpass_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.askpass_path.write_text("#!/bin/sh\nset -eu\n/usr/bin/cat -- \"$RELAY_SSH_PASSWORD_FILE\"\n", encoding="utf-8")
            os.chmod(self.askpass_path, 0o700)
        self.enabled = enabled
        self.agents_dir = state_dir / "agents"
        self.agents_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.agents_dir, 0o700)
        self.limit = AdjustableSemaphore(max_concurrent)
        self.lock = threading.Lock()
        self.db_write_lock = threading.Lock()
        self.max_per_node = max_per_node
        self.node_limits: dict[str, AdjustableSemaphore] = {}
        self.destination_condition = threading.Condition()
        self.destination_reservations: dict[str, tuple[str, str]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.stopping = threading.Event()
        if enabled:
            with self.connect() as db:
                db.execute(
                    "UPDATE tasks SET status = 'failed', error_message = ?, updated_at = ? WHERE status = 'transferring'",
                    ("控制服务重启，传输已中断；可点击重试继续", self.now()),
                )
            threading.Thread(target=self._cleanup_stale_authorizations, daemon=True, name="relay-key-cleanup").start()
            threading.Thread(target=self._schedule_loop, daemon=True, name="relay-schedule").start()

    @staticmethod
    def now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def start(self, task_id: str) -> bool:
        if self.stopping.is_set():
            return False
        if not self.enabled:
            return True
        with self.lock:
            if self.stopping.is_set() or task_id in self.runs:
                return False
            state: dict[str, Any] = {"cancel": threading.Event(), "process": None}
            self.runs[task_id] = state
        thread = threading.Thread(target=self._run, args=(task_id, state), daemon=True, name=f"relay-transfer-{task_id[:8]}")
        thread.start()
        return True

    def cancel(self, task_id: str) -> bool:
        with self.lock:
            state = self.runs.get(task_id)
            if not state:
                return False
            state["cancel"].set()
            process = state.get("process")
        if process and process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        return True

    def is_running(self, task_id: str) -> bool:
        with self.lock:
            return task_id in self.runs

    def shutdown(self, timeout: float = 40) -> bool:
        """Stop scheduling and let cancelled workers clean up before exit.

        Interrupted task records remain available; startup marks unfinished
        transfers failed so users can explicitly retry them.
        """
        with self.lock:
            self.stopping.set()
            task_ids = list(self.runs)
        for task_id in task_ids:
            self.cancel(task_id)
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                if not self.runs:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _schedule_loop(self) -> None:
        """Start persisted queued tasks once their requested time arrives."""
        while not self.stopping.is_set():
            try:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                with self.connect() as db:
                    rows = db.execute(
                        "SELECT id FROM tasks WHERE status = 'queued' AND schedule_at IS NOT NULL AND schedule_at <= ?",
                        (now,),
                    ).fetchall()
                for row in rows:
                    self.start(row["id"])
            except Exception:
                pass
            self.stopping.wait(10)

    def _load_task(self, task_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                """
                SELECT tasks.*,
                       source.host AS source_host, source.ssh_port AS source_port, source.username AS source_user,
                       source.status AS source_status, source_key.key_path AS source_key_path,
                       source_key.auth_type AS source_auth_type, source_key.password_path AS source_password_path,
                       destination.host AS destination_host, destination.ssh_port AS destination_port,
                       COALESCE(tasks.direct_host, destination.host) AS destination_transfer_host,
                       COALESCE(tasks.direct_port, destination.ssh_port) AS destination_transfer_port,
                       destination.username AS destination_user, destination.status AS destination_status,
                       destination_key.key_path AS destination_key_path,
                       destination_key.auth_type AS destination_auth_type, destination_key.password_path AS destination_password_path
                FROM tasks
                JOIN nodes AS source ON source.id = tasks.source_node_id
                JOIN credentials AS source_key ON source_key.id = source.credential_id
                JOIN nodes AS destination ON destination.id = tasks.destination_node_id
                JOIN credentials AS destination_key ON destination_key.id = destination.credential_id
                WHERE tasks.id = ?
                """,
                (task_id,),
            ).fetchone()

    def configure_limits(self, max_concurrent: int, max_per_node: int) -> None:
        self.limit.set_limit(max_concurrent)
        with self.lock:
            self.max_per_node = max_per_node
            node_limits = list(self.node_limits.values())
        for node_limit in node_limits:
            node_limit.set_limit(max_per_node)

    @staticmethod
    def _paths_overlap(left: str, right: str) -> bool:
        left = left.rstrip("/") or "/"
        right = right.rstrip("/") or "/"
        return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")

    def _acquire_destination_path(self, task_id: str, task: sqlite3.Row, cancel: threading.Event) -> bool:
        destination = (str(task["destination_node_id"]), str(task["destination_path"]).rstrip("/") or "/")
        with self.destination_condition:
            while not cancel.is_set():
                conflict = any(
                    node_id == destination[0] and self._paths_overlap(path, destination[1])
                    for node_id, path in self.destination_reservations.values()
                )
                if not conflict:
                    self.destination_reservations[task_id] = destination
                    return True
                self.destination_condition.wait(timeout=0.25)
        return False

    def _release_destination_path(self, task_id: str) -> None:
        with self.destination_condition:
            if self.destination_reservations.pop(task_id, None):
                self.destination_condition.notify_all()

    def _acquire_task_node_limits(
        self, task: sqlite3.Row | dict[str, Any], cancel: threading.Event,
    ) -> list[AdjustableSemaphore] | None:
        """Reserve both endpoint nodes using the configurable per-node limit."""
        node_ids = sorted({
            str(task[column]) for column in ("source_node_id", "destination_node_id")
            if task[column]
        })
        with self.lock:
            limits = [self.node_limits.setdefault(node_id, AdjustableSemaphore(self.max_per_node)) for node_id in node_ids]
        acquired: list[AdjustableSemaphore] = []
        for node_limit in limits:
            if not node_limit.acquire(cancel):
                for acquired_limit in reversed(acquired):
                    acquired_limit.release()
                return None
            acquired.append(node_limit)
        return acquired

    def _update(self, task_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = self.now()
        assignments = ", ".join(f"{column} = ?" for column in values)
        for attempt in range(3):
            try:
                with self.db_write_lock:
                    with self.connect() as db:
                        db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*values.values(), task_id))
                        if values.get("status") in ("completed", "failed"):
                            task = db.execute("SELECT owner_id, name FROM tasks WHERE id = ?", (task_id,)).fetchone()
                            if task:
                                db.execute(
                                    "INSERT INTO audit_events(actor_id, action, entity_type, entity_id, detail, created_at) VALUES (?, ?, 'task', ?, ?, ?)",
                                    (task["owner_id"], f"task_{values['status']}", task_id, f"传输任务 {task['name']}：{values['status']}", self.now()),
                                )
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 2:
                    raise
                time.sleep(0.25 * (attempt + 1))

    def _append_log(self, current: str, line: str) -> str:
        cleaned = line.replace("\x00", "").strip()
        if not cleaned:
            return current
        return (current + cleaned + "\n")[-12_000:]

    @staticmethod
    def _uses_password(row: sqlite3.Row | dict[str, Any]) -> bool:
        try:
            return row["auth_type"] == "password"
        except (KeyError, IndexError):
            return False

    def _ssh_env(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, str]:
        env = {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": str(self.state_dir)}
        if self._uses_password(row):
            env.update({
                "DISPLAY": "relay:0",
                "SSH_ASKPASS_REQUIRE": "force",
                "SSH_ASKPASS": str(self.askpass_path),
                "RELAY_SSH_PASSWORD_FILE": row["password_path"],
            })
        return env

    def _task_source_auth(self, task: sqlite3.Row) -> dict[str, Any]:
        return {"auth_type": task["source_auth_type"], "password_path": task["source_password_path"], "key_path": task["source_key_path"]}

    def _task_destination_auth(self, task: sqlite3.Row) -> dict[str, Any]:
        return {"auth_type": task["destination_auth_type"], "password_path": task["destination_password_path"], "key_path": task["destination_key_path"]}

    def _ssh_prefix(self, row: sqlite3.Row | dict[str, Any], *, port: Any, key_path: str | None = None) -> list[str]:
        if self._uses_password(row):
            command = ["/usr/bin/ssh", "-F", "/dev/null", "-p", str(port),
                       "-o", "BatchMode=no", "-o", "PasswordAuthentication=yes", "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"]
            return (["/usr/bin/setsid", "-w", *command] if Path("/usr/bin/setsid").exists() else command)
        return ["/usr/bin/ssh", "-F", "/dev/null", "-i", key_path or row["key_path"], "-p", str(port),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no"]

    def _destination_ssh_command(self, task: sqlite3.Row, remote_command: str) -> list[str]:
        return self._ssh_prefix(self._task_destination_auth(task), port=task["destination_port"], key_path=task["destination_key_path"]) + [
            "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=2", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts_path}", "-o", "LogLevel=ERROR",
            f"{task['destination_user']}@{task['destination_host']}", remote_command,
        ]

    def _node_ssh_command(self, node: sqlite3.Row, remote_command: str) -> list[str]:
        return self._ssh_prefix(node, port=node["ssh_port"]) + [
            "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=2", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts_path}", "-o", "LogLevel=ERROR",
            f"{node['username']}@{node['host']}", remote_command,
        ]

    def _source_ssh_command(self, task: sqlite3.Row, remote_command: str) -> list[str]:
        return self._ssh_prefix(self._task_source_auth(task), port=task["source_port"], key_path=task["source_key_path"]) + [
            "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=2", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts_path}", "-o", "LogLevel=ERROR",
            f"{task['source_user']}@{task['source_host']}", remote_command,
        ]

    def _measure_source_size(self, task: sqlite3.Row) -> int:
        path = shlex.quote(task["source_path"])
        remote_command = self._source_inspection_command(path)
        result = subprocess.run(
            self._source_ssh_command(task, remote_command), capture_output=True, text=True,
            timeout=45, check=False, env=self._ssh_env(self._task_source_auth(task)),
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr.strip() or result.stdout.strip() or "无法读取源数据大小")[-500:])
        total_bytes, _ = self._parse_source_inspection(result.stdout)
        return total_bytes

    @staticmethod
    def _source_inspection_command(path: str) -> str:
        """Report a source path's kind and size without reading its content."""
        return (
            "set -eu; "
            f"if sudo -n /usr/bin/test -d {path}; then "
            "printf '%s ' directory; "
            f"sudo -n /usr/bin/du -sb -- {path} | /usr/bin/cut -f1; "
            f"elif sudo -n /usr/bin/test -f {path}; then "
            "printf '%s ' file; "
            f"sudo -n /usr/bin/stat -c %s -- {path}; "
            "else "
            "printf '%s\\n' '源路径不存在、不是普通文件或无权访问'; exit 66; "
            "fi"
        )

    @staticmethod
    def _parse_source_inspection(stdout: str) -> tuple[int, bool]:
        line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in {"directory", "file"} or not parts[1].isdecimal():
            raise RuntimeError("无法读取源数据大小")
        return int(parts[1]), parts[0] == "directory"

    def preflight(self, source: sqlite3.Row, source_path: str, destination: sqlite3.Row, destination_path: str) -> tuple[int, bool]:
        """Check the target directory and return source size plus its directory flag."""
        source_quoted = shlex.quote(source_path)
        source_command = self._source_inspection_command(source_quoted)
        result = subprocess.run(
            self._node_ssh_command(source, source_command), capture_output=True, text=True,
            timeout=45, check=False, env=self._ssh_env(source),
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr.strip() or result.stdout.strip() or "无法读取源数据大小")[-500:])
        source_size, source_is_directory = self._parse_source_inspection(result.stdout)
        self._check_destination_directory(destination, destination_path)
        return source_size, source_is_directory

    def _check_destination_directory(self, destination: sqlite3.Row, destination_path: str) -> None:
        destination_quoted = shlex.quote(destination_path)
        destination_command = (
            "set -eu; "
            f"sudo -n /usr/bin/test -d {destination_quoted} || "
            "{ printf '%s\\n' '目标目录不存在或无权访问'; exit 67; }; "
            f"sudo -n /usr/bin/test -w {destination_quoted} || "
            "{ printf '%s\\n' '目标目录不可写'; exit 67; }"
        )
        result = subprocess.run(
            self._node_ssh_command(destination, destination_command), capture_output=True, text=True,
            timeout=25, check=False, env=self._ssh_env(destination),
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr.strip() or result.stdout.strip() or "无法访问目标目录")[-500:])

    def _cleanup_stale_authorizations(self) -> None:
        with self.connect() as db:
            nodes = db.execute(
                """
                SELECT nodes.host, nodes.ssh_port,
                       nodes.username, credentials.key_path,
                       credentials.auth_type, credentials.password_path
                FROM nodes JOIN credentials ON credentials.id = nodes.credential_id
                """
            ).fetchall()
        command = (
            "test ! -f \"$HOME/.ssh/authorized_keys\" || "
            "/usr/bin/sed -i '/ relay-task-[0-9a-f-]\\+$/d' \"$HOME/.ssh/authorized_keys\""
        )
        for node in nodes:
            try:
                subprocess.run(
                    self._node_ssh_command(node, command), capture_output=True, timeout=15, check=False,
                    env=self._ssh_env(node),
                )
            except (OSError, subprocess.TimeoutExpired):
                continue

    def _generate_task_key(self, task_id: str) -> tuple[Path, bytes, str]:
        key_path = self.agents_dir / f"{task_id}.key"
        public_path = Path(f"{key_path}.pub")
        key_path.unlink(missing_ok=True)
        public_path.unlink(missing_ok=True)
        result = subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"relay-task-{task_id}", "-f", str(key_path)],
            capture_output=True, text=True, timeout=10, check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": str(self.state_dir)},
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "无法生成临时传输密钥")
        private_key = key_path.read_bytes()
        public_parts = public_path.read_text(encoding="utf-8").split()
        if len(public_parts) < 2:
            raise RuntimeError("临时传输公钥格式不正确")
        return key_path, private_key, f"{public_parts[0]} {public_parts[1]}"

    def _authorize_destination(self, task: sqlite3.Row, public_key: str) -> None:
        task_marker = f"relay-task-{task['id']}"
        forced_command = f"/usr/local/bin/relay-rrsync -wo '{task['destination_path']}'"
        # A task may route through a private address, so its observed source IP is
        # not necessarily the node's public management address.  The key is still
        # short-lived, restricted, and forced into this task's destination path.
        authorized_line = (
            f"command=\"{forced_command}\",restrict "
            f"{public_key} {task_marker}\n"
        )
        encoded_line = base64.b64encode(authorized_line.encode("utf-8")).decode("ascii")
        encoded_wrapper = base64.b64encode(RESTRICTED_RSYNC_WRAPPER).decode("ascii")
        remote_command = (
            "set -eu; umask 077; "
            f"printf %s {shlex.quote(encoded_wrapper)} | /usr/bin/base64 -d | "
            "/usr/bin/sudo -n /usr/bin/tee /usr/local/bin/relay-rrsync >/dev/null; "
            "/usr/bin/sudo -n /usr/bin/chmod 0755 /usr/local/bin/relay-rrsync; "
            "/usr/bin/mkdir -p \"$HOME/.ssh\"; /usr/bin/chmod 700 \"$HOME/.ssh\"; "
            "/usr/bin/touch \"$HOME/.ssh/authorized_keys\"; /usr/bin/chmod 600 \"$HOME/.ssh/authorized_keys\"; "
            f"/usr/bin/sed -i '/ {task_marker}$/d' \"$HOME/.ssh/authorized_keys\"; "
            f"printf %s {shlex.quote(encoded_line)} | /usr/bin/base64 -d >> \"$HOME/.ssh/authorized_keys\""
        )
        result = subprocess.run(
            self._destination_ssh_command(task, remote_command), capture_output=True, text=True,
            timeout=25, check=False, env=self._ssh_env(self._task_destination_auth(task)),
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr.strip() or result.stdout.strip() or "目标节点授权失败")[-500:])

    def _revoke_destination(self, task: sqlite3.Row) -> None:
        task_marker = f"relay-task-{task['id']}"
        remote_command = (
            "test ! -f \"$HOME/.ssh/authorized_keys\" || "
            f"/usr/bin/sed -i '/ {task_marker}$/d' \"$HOME/.ssh/authorized_keys\""
        )
        subprocess.run(
            self._destination_ssh_command(task, remote_command), capture_output=True, timeout=20, check=False,
            env=self._ssh_env(self._task_destination_auth(task)),
        )

    def _run(self, task_id: str, state: dict[str, Any]) -> None:
        task: sqlite3.Row | None = None
        task_key_path: Path | None = None
        task_public_path: Path | None = None
        authorized = False
        acquired = False
        acquired_node_limits: list[AdjustableSemaphore] = []
        destination_reserved = False
        log_tail = ""
        try:
            task = self._load_task(task_id)
            if not task:
                raise RuntimeError("任务节点或 SSH 凭据不存在")
            if not self._acquire_destination_path(task_id, task, state["cancel"]):
                return
            destination_reserved = True
            node_limits = self._acquire_task_node_limits(task, state["cancel"])
            if node_limits is None:
                return
            acquired_node_limits = node_limits
            acquired = self.limit.acquire(state["cancel"])
            if not acquired:
                return
            if task["source_status"] != "online" or task["destination_status"] != "online":
                raise RuntimeError("源节点和目标节点必须保持在线")
            source_size = self._measure_source_size(task)
            self._check_destination_directory({
                **self._task_destination_auth(task), "ssh_port": task["destination_port"],
                "username": task["destination_user"], "host": task["destination_host"],
            }, task["destination_path"])
            resume_base = min(max(0, task["progress_base_bytes"] or 0), source_size)
            self._update(
                task_id, total_bytes=source_size, transferred_bytes=resume_base,
                progress=min(99, resume_base * 100 // source_size) if source_size else 0,
            )
            max_size_bytes = task["max_size_bytes"]
            if max_size_bytes and source_size > max_size_bytes:
                raise RuntimeError(
                    f"源数据大小 {format_data_size(source_size)} 超过任务上限 {format_data_size(max_size_bytes)}"
                )
            destination_hosts = _retarget_known_host_lines(_known_host_lines(
                self.known_hosts_path, task["destination_host"], task["destination_port"]
            ), task["destination_transfer_host"], task["destination_transfer_port"])
            task_key_path, private_key, public_key = self._generate_task_key(task_id)
            task_public_path = Path(f"{task_key_path}.pub")
            self._authorize_destination(task, public_key)
            authorized = True
            remote_command = build_remote_command(task, task, destination_hosts, len(private_key))
            command = self._source_ssh_command(task, remote_command)
            self._update(
                task_id, status="transferring", error_message=None, started_at=self.now(),
                speed_bps=None, eta_seconds=None,
            )
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=self._ssh_env(self._task_source_auth(task)), bufsize=0,
            )
            with self.lock:
                state["process"] = process
            assert process.stdin is not None
            try:
                process.stdin.write(private_key)
                process.stdin.close()
            except BrokenPipeError:
                pass
            buffer = b""
            last_progress_write = 0.0
            last_log_write = 0.0
            log_dirty = False
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                pieces = re.split(rb"[\r\n]+", buffer)
                buffer = pieces.pop()
                for raw_line in pieces:
                    line = raw_line.decode("utf-8", errors="replace")
                    progress = parse_progress_line(line)
                    if progress:
                        now = time.monotonic()
                        if not state["cancel"].is_set() and (now - last_progress_write >= 0.4 or progress["progress"] == 100):
                            update = overall_progress(source_size, progress, resume_base)
                            if log_dirty:
                                update["log_tail"] = log_tail
                                log_dirty = False
                                last_log_write = now
                            self._update(task_id, **update)
                            last_progress_write = now
                    else:
                        log_tail = self._append_log(log_tail, line)
                        log_dirty = True
                        now = time.monotonic()
                        if now - last_log_write >= 1:
                            self._update(task_id, log_tail=log_tail)
                            log_dirty = False
                            last_log_write = now
            if buffer:
                log_tail = self._append_log(log_tail, buffer.decode("utf-8", errors="replace"))
                log_dirty = True
            if log_dirty:
                self._update(task_id, log_tail=log_tail)
            return_code = process.wait()
            if state["cancel"].is_set():
                return
            if return_code == 0:
                if task["verify_after_transfer"]:
                    self._update(
                        task_id, status="transferring", progress=99, speed_bps=0, eta_seconds=None,
                        verification_status="running", verification_message="正在校验源端与目标端内容",
                    )
                    verification_error, verification_log = self._verify_content(
                        task, destination_hosts, private_key, state,
                    )
                    log_tail = self._append_log(log_tail, verification_log)
                    if state["cancel"].is_set():
                        return
                    if verification_error:
                        self._update(
                            task_id, status="failed", speed_bps=0, eta_seconds=None,
                            error_message=verification_error, verification_status="failed",
                            verification_message=verification_error, log_tail=log_tail,
                        )
                        return
                self._update(
                    task_id, status="completed", progress=100, total_bytes=source_size,
                    transferred_bytes=source_size,
                    progress_base_bytes=source_size,
                    speed_bps=0, eta_seconds=0, completed_at=self.now(), error_message=None,
                    verification_status="passed" if task["verify_after_transfer"] else None,
                    verification_message="内容校验通过" if task["verify_after_transfer"] else None,
                    log_tail=log_tail,
                )
            else:
                error_lines = [line for line in log_tail.strip().splitlines() if line]
                error = error_lines[-1] if error_lines else f"rsync 退出码 {return_code}"
                self._update(task_id, status="failed", speed_bps=0, eta_seconds=None, error_message=error[-500:])
        except Exception as exc:
            if not state["cancel"].is_set():
                self._update(task_id, status="failed", speed_bps=0, eta_seconds=None, error_message=str(exc)[-500:])
        finally:
            process = state.get("process")
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            if authorized and task:
                self._revoke_destination(task)
            if task_key_path:
                task_key_path.unlink(missing_ok=True)
            if task_public_path:
                task_public_path.unlink(missing_ok=True)
            if acquired:
                self.limit.release()
            for node_limit in reversed(acquired_node_limits):
                node_limit.release()
            if destination_reserved:
                self._release_destination_path(task_id)
            with self.lock:
                if self.runs.get(task_id) is state:
                    self.runs.pop(task_id, None)

    def _verify_content(
        self, task: sqlite3.Row, destination_hosts: str, private_key: bytes, state: dict[str, Any],
    ) -> tuple[str | None, str]:
        """Run a read-only rsync checksum pass and return a concise difference report."""
        command = self._source_ssh_command(
            task, build_remote_command(task, task, destination_hosts, len(private_key), verify=True),
        )
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=self._ssh_env(self._task_source_auth(task)), bufsize=0,
        )
        with self.lock:
            state["process"] = process
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(private_key)
            process.stdin.close()
        except BrokenPipeError:
            pass
        output = bytearray()
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > 12_000:
                del output[:-12_000]
        return_code = process.wait()
        text = output.decode("utf-8", errors="replace").strip()
        if state["cancel"].is_set():
            return None, text
        if return_code != 0:
            return (text.splitlines()[-1] if text else f"内容校验命令退出码 {return_code}"), text
        if text:
            lines = text.splitlines()
            return f"内容校验发现 {len(lines)} 项差异，未改动文件", text
        return None, "内容校验通过（未发现内容差异）"
