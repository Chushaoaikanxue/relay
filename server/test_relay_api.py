import http.client
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from relay_api import RelayApp, RelayServer
from relay_transfer import build_local_command, build_remote_command, overall_progress, parse_progress_line


class RelayApiTest(unittest.TestCase):
    def seed_mode_nodes(self):
        status, _ = self.request("POST", "/relay/api/setup", {
            "username": "admin", "display_name": "Test", "password": "test-only-long-password",
        })
        self.assertEqual(status, 201)
        with self.server.app.connect() as db:
            db.execute("INSERT INTO credentials(id,name,fingerprint,key_path,created_at,owner_id) VALUES ('test-key','test','test','/tmp/test-only-key','2026-01-01',1)")
            for index in (1, 2):
                db.execute("INSERT INTO nodes(id,name,status,host,ssh_port,username,credential_id,created_at) VALUES (?,?,'online',?,2222,'test','test-key','2026-01-01')", (f"node-{index}", f"Node {index}", f"127.0.0.{index}"))
        self.server.app.transfers.preflight = Mock(return_value=(1024, True))
        self.server.app.transfers.check_local_paths = Mock()
        return {"source_node_id": "node-1", "destination_node_id": "node-2", "source_path": "/data/source/", "destination_path": "/archive/"}

    def test_explicit_modes_and_legacy_compatibility(self):
        body = self.seed_mode_nodes()
        for mode, additions in (("public", {}), ("private", {"direct_host": "10.0.0.2"}), ("local", {"destination_node_id": "node-1"})):
            status, result = self.request("POST", "/relay/api/tasks", {**body, "transfer_mode": mode, **additions})
            self.assertEqual(status, 201, result)
            task = result["task"]
            self.assertEqual(task["transfer_mode"], mode)
            self.assertEqual(task["source_path"], "/data/source")
            self.assertEqual(task["direct_port"], 22 if mode == "private" else None)
        self.server.app.transfers.check_local_paths.assert_called_once()
        status, result = self.request("POST", "/relay/api/tasks", {**body, "direct_host": "10.0.0.2"})
        self.assertEqual(status, 201)
        self.assertEqual(result["task"]["transfer_mode"], "legacy")
        self.assertEqual(result["task"]["source_path"], "/data/source/")
        self.assertEqual(result["task"]["direct_port"], 2222)

    def test_modes_reject_mixed_endpoints_and_deletion(self):
        body = self.seed_mode_nodes()
        cases = [
            {"transfer_mode": "auto"},
            {"transfer_mode": "private"},
            {"transfer_mode": "private", "direct_host": "some-node"},
            {"transfer_mode": "private", "direct_host": "10.0.0.2", "direct_port": 65536},
            {"transfer_mode": "public", "direct_host": "10.0.0.2"},
            {"transfer_mode": "public", "destination_node_id": "node-1"},
            {"transfer_mode": "local"},
            {"transfer_mode": "local", "destination_node_id": "node-1", "direct_port": 22},
            {"transfer_mode": "local", "destination_node_id": "node-1", "delete_enabled": True},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                status, result = self.request("POST", "/relay/api/tasks", {**body, **extra})
                self.assertEqual(status, 400, result)
        self.server.app.transfers.preflight.assert_not_called()

    def test_cancel_and_retry_preserve_mode_and_do_not_restart_live_worker(self):
        body = self.seed_mode_nodes()
        status, result = self.request("POST", "/relay/api/tasks", {**body, "transfer_mode": "local", "destination_node_id": "node-1"})
        self.assertEqual(status, 201, result)
        task_id = result["task"]["id"]
        manager = self.server.app.transfers
        manager.runs[task_id] = {"cancel": threading.Event(), "process": None}
        status, result = self.request("POST", f"/relay/api/tasks/{task_id}/cancel", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["task"]["status"], "cancelling")
        manager._update(task_id, status="completed", progress=100)
        status, _ = self.request("POST", f"/relay/api/tasks/{task_id}/retry", {})
        self.assertEqual(status, 400)
        manager.runs.clear()
        _, listing = self.request("GET", "/relay/api/tasks")
        self.assertEqual(listing["tasks"][0]["status"], "cancelled")
        status, result = self.request("POST", f"/relay/api/tasks/{task_id}/retry", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["task"]["status"], "queued")
        self.assertEqual(result["task"]["transfer_mode"], "local")

    def test_local_path_checks_resolve_symlinks_and_final_filename(self):
        manager = self.server.app.transfers
        node = {"key_path": "/test-key", "ssh_port": 22, "username": "test", "host": "127.0.0.1"}
        for output in ("/data/src\n/data/src/sub\n/data/src/sub/src\n", "/data/file\n/data\n/data/file\n", "/data/src\n/alias\n/data/src\n"):
            with patch("relay_transfer.subprocess.run", return_value=Mock(returncode=0, stdout=output)):
                with self.assertRaisesRegex(RuntimeError, "不能相同或互相包含"):
                    manager.check_local_paths(node, "/data/src", "/alias")
        with patch("relay_transfer.subprocess.run", return_value=Mock(returncode=0, stdout="/data/src\n/archive\n/archive/src\n")):
            manager.check_local_paths(node, "/data/src", "/archive")

    def test_unconfirmed_stop_blocks_deletion_and_unsafe_retry(self):
        body = self.seed_mode_nodes()
        _, result = self.request("POST", "/relay/api/tasks", {**body, "transfer_mode": "local", "destination_node_id": "node-1"})
        task_id = result["task"]["id"]
        with self.server.app.connect() as db:
            db.execute("UPDATE tasks SET status='failed',stop_reason='unconfirmed' WHERE id=?", (task_id,))
        manager = self.server.app.transfers
        with patch.object(manager, "_stop_source", side_effect=RuntimeError("停止尚未确认")):
            status, _ = self.request("POST", f"/relay/api/tasks/{task_id}/retry", {})
            self.assertEqual(status, 400)
        status, _ = self.request("DELETE", f"/relay/api/tasks/{task_id}")
        self.assertEqual(status, 400)
        with self.assertRaisesRegex(RuntimeError, "尚未确认停止"):
            manager._acquire_destination_path("other-task", {"destination_node_id": "node-1", "destination_path": "/archive/sub"}, threading.Event())
        with patch.object(manager, "_stop_source"):
            status, result = self.request("POST", f"/relay/api/tasks/{task_id}/retry", {})
            self.assertEqual(status, 200)
            self.assertEqual(result["task"]["status"], "queued")

    def test_local_command_supports_partial_reuse_and_read_only_verification(self):
        task = {"id": "test-mode", "source_path": "/data/source space", "destination_path": "/archive/", "bandwidth_limit_kbps": 1024}
        command = build_local_command(task)
        self.assertIn("--no-whole-file", command)
        self.assertIn("--partial-dir=.relay-partial-test-mode", command)
        self.assertNotIn("--delete", command)
        self.assertNotIn("/usr/bin/ssh", command)
        verify = build_local_command(task, verify=True)
        self.assertIn("-rcln", verify)
        self.assertNotIn("--bwlimit", verify)

    def test_shutdown_rejects_new_transfers(self):
        manager = self.server.app.transfers
        self.assertTrue(manager.shutdown(timeout=0))
        self.assertFalse(manager.start("should-not-start"))

    def test_shutdown_cancels_workers_and_has_bounded_wait(self):
        manager = self.server.app.transfers
        process = Mock()
        process.poll.return_value = None
        cancel = threading.Event()
        manager.runs["test-worker"] = {"cancel": cancel, "process": process}
        self.assertFalse(manager.shutdown(timeout=0))
        self.assertTrue(cancel.is_set())
        process.send_signal.assert_called_once()
        self.assertFalse(manager.start("another-worker"))
        manager.runs.clear()
        self.assertTrue(manager.shutdown(timeout=0))

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self.tempdir.name) / "relay.db")
        self.server = RelayServer(("127.0.0.1", 0), RelayApp(
            db_path, "http://127.0.0.1", secure_cookie=False, enable_transfers=False,
        ))
        self.server.app.test_node_async = lambda node_ids: None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.cookie = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"X-Relay-Request": "1", "Origin": "http://127.0.0.1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        connection.close()
        return response.status, data

    def test_setup_login_and_task_flow(self):
        status, data = self.request("GET", "/relay/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(data["setup_required"])

        status, data = self.request("POST", "/relay/api/setup", {
            "username": "admin",
            "display_name": "Test Admin",
            "password": "correct-horse-battery-staple",
        })
        self.assertEqual(status, 201)
        self.assertEqual(data["user"]["username"], "admin")

        key_path = Path(self.tempdir.name) / "test_ed25519"
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)], check=True)
        status, data = self.request("POST", "/relay/api/nodes/bulk", {
            "name_prefix": "test-node",
            "hosts": "127.0.0.1\n127.0.0.2",
            "ssh_port": 22,
            "username": "relay_test",
            "credential_name": "test key",
            "private_key": key_path.read_text(),
        })
        self.assertEqual(status, 201)
        self.assertEqual(data["created"], 2)
        self.assertTrue(data["fingerprint"].startswith("SHA256:"))
        node_ids = data["node_ids"]

        status, data = self.request("GET", "/relay/api/nodes")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["nodes"]), 2)
        self.assertNotIn("key_path", data["nodes"][0])
        self.assertTrue(all(node["status"] == "pending" for node in data["nodes"]))

        with self.server.app.connect() as db:
            db.execute("UPDATE nodes SET status = 'online'")
        self.server.app.transfers.preflight = lambda *args: (12 * 1024 * 1024, True)

        status, data = self.request("POST", "/relay/api/tasks", {
            "name": "volume-copy",
            "source_node_id": node_ids[0],
            "source_path": "/srv/source/",
            "destination_node_id": node_ids[1],
            "destination_path": "/srv/destination/",
            "bandwidth_limit_mbps": 50,
            "direct_host": "10.10.0.2",
            "direct_port": 2222,
            "delete_enabled": False,
            "verify_after_transfer": True,
        })
        self.assertEqual(status, 201)
        self.assertEqual(data["task"]["status"], "queued")
        self.assertEqual(data["task"]["source_path"], "/srv/source/")
        self.assertEqual(data["task"]["bandwidth_limit_kbps"], 50 * 1024)
        self.assertEqual(data["task"]["max_size_bytes"], 100 * 1024 * 1024 * 1024)
        self.assertEqual(data["task"]["direct_host"], "10.10.0.2")
        self.assertEqual(data["task"]["direct_port"], 2222)
        self.assertTrue(data["task"]["verify_after_transfer"])
        self.assertEqual(data["task"]["size"], "12.0 MB")
        task_id = data["task"]["id"]

        self.server.app.transfers.preflight = lambda *args: (1024, False)
        status, data = self.request("POST", "/relay/api/tasks", {
            "name": "single-file-copy",
            "source_node_id": node_ids[0],
            "source_path": "/srv/exports/report.zip",
            "destination_node_id": node_ids[1],
            "destination_path": "/srv/destination/",
            "schedule_at": "2030-01-01T00:00:00+00:00",
        })
        self.assertEqual(status, 201)
        self.assertEqual(data["task"]["source_path"], "/srv/exports/report.zip")
        file_task_id = data["task"]["id"]
        status, data = self.request("POST", "/relay/api/tasks", {
            "source_node_id": node_ids[0],
            "source_path": "/srv/exports/report.zip",
            "destination_node_id": node_ids[1],
            "destination_path": "/srv/destination/",
            "delete_enabled": True,
        })
        self.assertEqual(status, 400)
        self.assertIn("镜像删除仅支持目录", data["error"])
        status, data = self.request("DELETE", f"/relay/api/tasks/{file_task_id}")
        self.assertEqual(status, 200)
        self.server.app.transfers.preflight = lambda *args: (12 * 1024 * 1024, True)

        status, data = self.request("POST", "/relay/api/tasks", {
            "name": "scheduled-copy",
            "source_node_id": node_ids[0],
            "source_path": "/srv/source/",
            "destination_node_id": node_ids[1],
            "destination_path": "/srv/destination/",
            "schedule_at": "2030-01-01T00:00:00+00:00",
            "max_size_gb": 0,
        })
        self.assertEqual(status, 201)
        self.assertTrue(data["task"]["scheduled"])
        self.assertEqual(data["task"]["max_size_bytes"], 0)
        scheduled_task_id = data["task"]["id"]

        self.server.app.transfers.preflight = lambda *args: (2 * 1024 * 1024 * 1024, True)
        status, data = self.request("POST", "/relay/api/tasks", {
            "source_node_id": node_ids[0],
            "source_path": "/srv/source/",
            "destination_node_id": node_ids[1],
            "destination_path": "/srv/destination/",
            "max_size_gb": 1,
        })
        self.assertEqual(status, 400)
        self.assertIn("源数据总大小", data["error"])

        def missing_destination(*args):
            raise RuntimeError("目标目录不存在或无权访问")
        self.server.app.transfers.preflight = missing_destination
        status, data = self.request("POST", "/relay/api/tasks", {
            "source_node_id": node_ids[0],
            "source_path": "/srv/source/",
            "destination_node_id": node_ids[1],
            "destination_path": "/srv/missing/",
        })
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "目标目录不存在或无权访问")
        self.server.app.transfers.preflight = lambda *args: (12 * 1024 * 1024, True)

        status, data = self.request("POST", "/relay/api/tasks", {
            "source_node_id": node_ids[0],
            "source_path": "relative/path",
            "destination_node_id": node_ids[1],
            "destination_path": "/srv/destination/",
        })
        self.assertEqual(status, 400)

        status, data = self.request("POST", "/relay/api/tasks", {
            "source_node_id": node_ids[0],
            "source_path": "/srv/source/",
            "destination_node_id": node_ids[1],
            "destination_path": "/srv/destination/",
            "max_size_gb": -1,
        })
        self.assertEqual(status, 400)

        status, data = self.request("GET", "/relay/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["tasks"]), 2)
        task_details = next(task for task in data["tasks"] if task["id"] == task_id)
        self.assertEqual(task_details["source_node"]["host"], "127.0.0.1")
        self.assertEqual(task_details["direct_host"], "10.10.0.2")
        self.assertEqual(task_details["direct_port"], 2222)
        self.assertTrue(task_details["verify_after_transfer"])

        status, data = self.request("GET", "/relay/api/transfer-settings")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"max_concurrent": 4, "max_per_node": 2})
        status, data = self.request("POST", "/relay/api/transfer-settings", {
            "max_concurrent": 6, "max_per_node": 3,
        })
        self.assertEqual(status, 200)
        self.assertEqual(data, {"max_concurrent": 6, "max_per_node": 3})

        status, data = self.request("DELETE", f"/relay/api/tasks/{scheduled_task_id}")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, data = self.request("GET", "/relay/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["tasks"]), 1)

        status, data = self.request("POST", f"/relay/api/tasks/{task_id}/pause", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["task"]["status"], "paused")

        status, data = self.request("POST", f"/relay/api/tasks/{task_id}/resume", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["task"]["status"], "queued")

        status, data = self.request("DELETE", f"/relay/api/nodes/{node_ids[0]}")
        self.assertEqual(status, 400)

        status, data = self.request("POST", f"/relay/api/tasks/{task_id}/pause", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["task"]["status"], "paused")

        status, data = self.request("DELETE", f"/relay/api/nodes/{node_ids[0]}")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, data = self.request("GET", "/relay/api/nodes")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["nodes"]), 1)

        status, _ = self.request("POST", "/relay/api/logout", {})
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/relay/api/tasks")
        self.assertEqual(status, 401)

    def test_startup_cleanup_preserves_password_auth(self):
        status, _ = self.request("POST", "/relay/api/setup", {
            "username": "admin", "display_name": "Admin", "password": "test-password-12345",
        })
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/relay/api/nodes/bulk", {
            "name": "password-node", "hosts": "127.0.0.1", "ssh_port": 22,
            "username": "relay_test", "auth_type": "password", "ssh_password": "test-ssh-password",
        })
        self.assertEqual(status, 201)
        with patch("relay_transfer.subprocess.run") as run:
            self.server.app.transfers._cleanup_stale_authorizations()
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertIn("PreferredAuthentications=password", args[0])
        self.assertIn("relay_test@127.0.0.1", args[0])
        self.assertIn("RELAY_SSH_PASSWORD_FILE", kwargs["env"])

    def test_transfer_destination_check_preserves_password_auth(self):
        manager = self.server.app.transfers
        task = {
            "status": "queued", "transfer_mode": "public",
            "source_node_id": "source", "destination_node_id": "destination",
            "source_status": "online", "destination_status": "online",
            "destination_auth_type": "password", "destination_password_path": "/test/password",
            "destination_key_path": "/test/password", "destination_port": 22,
            "destination_user": "relay", "destination_host": "127.0.0.1",
            "destination_path": "/srv/target",
        }
        with patch.object(manager, "_load_task", return_value=task), \
             patch.object(manager, "_measure_source_size", return_value=1024), \
             patch.object(manager, "_check_destination_directory", side_effect=RuntimeError("inspection stopped")) as check, \
             patch.object(manager, "_update"):
            manager._run("test-task", {"cancel": threading.Event(), "process": None})
        destination = check.call_args.args[0]
        self.assertEqual(destination["auth_type"], "password")
        self.assertEqual(destination["password_path"], "/test/password")

    def test_progress_parser(self):
        parsed = parse_progress_line("  1,048,576  50%   12.50MB/s    0:00:42 (xfr#4, to-chk=6/10)")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["transferred_bytes"], 1_048_576)
        self.assertEqual(parsed["total_bytes"], 2_097_152)
        self.assertEqual(parsed["speed_bps"], 12_500_000)
        self.assertEqual(parsed["eta_seconds"], 42)
        self.assertEqual(parsed["file_count"], 10)

    def test_tasks_sharing_a_node_wait_for_each_other(self):
        manager = self.server.app.transfers
        manager.configure_limits(max_concurrent=4, max_per_node=1)
        cancel = threading.Event()
        first_locks = manager._acquire_task_node_limits({
            "source_node_id": "node-a", "destination_node_id": "node-b",
        }, cancel)
        self.assertIsNotNone(first_locks)

        waiting_locks = []
        waiter = threading.Thread(target=lambda: waiting_locks.append(manager._acquire_task_node_limits({
            "source_node_id": "node-a", "destination_node_id": "node-c",
        }, cancel)))
        waiter.start()
        time.sleep(0.05)
        self.assertTrue(waiter.is_alive())

        for node_lock in reversed(first_locks or []):
            node_lock.release()
        waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(len(waiting_locks), 1)
        self.assertIsNotNone(waiting_locks[0])
        for node_lock in reversed(waiting_locks[0] or []):
            node_lock.release()

    def test_overlapping_destination_paths_conflict(self):
        self.assertTrue(self.server.app.transfers._paths_overlap("/mnt/archive", "/mnt/archive"))
        self.assertTrue(self.server.app.transfers._paths_overlap("/mnt/archive", "/mnt/archive/daily"))
        self.assertFalse(self.server.app.transfers._paths_overlap("/mnt/archive-a", "/mnt/archive-b"))

    def test_checksum_verification_command_is_read_only(self):
        task = {
            "id": "test-task", "delete_enabled": True, "bandwidth_limit_kbps": 1024,
            "source_path": "/srv/source/",
        }
        destination = {"destination_transfer_port": 22, "destination_transfer_host": "10.0.0.2", "destination_user": "relay"}
        command = build_remote_command(task, destination, "10.0.0.2 ssh-ed25519 AAAA\n", 32, verify=True)
        self.assertIn("-rcln", command)
        self.assertNotIn("--delete", command)
        self.assertNotIn("--bwlimit", command)

    def test_source_inspection_accepts_files_and_directories(self):
        manager = self.server.app.transfers
        self.assertEqual(manager._parse_source_inspection("file 1024\n"), (1024, False))
        self.assertEqual(manager._parse_source_inspection("directory 4096\n"), (4096, True))
        with self.assertRaises(RuntimeError):
            manager._parse_source_inspection("directory not-a-size\n")

    def test_overall_progress_uses_fixed_preflight_size(self):
        update = overall_progress(100_000_000_000, {
            "transferred_bytes": 17_400_000_000,
            "total_bytes": 17_600_000_000,
            "progress": 99,
            "speed_bps": 15_000_000,
            "eta_seconds": 1,
            "file_count": 85_302,
        })
        self.assertEqual(update["total_bytes"], 100_000_000_000)
        self.assertEqual(update["transferred_bytes"], 17_400_000_000)
        self.assertEqual(update["progress"], 17)
        self.assertEqual(update["eta_seconds"], 5_506)
        self.assertEqual(update["file_count"], 85_302)

        resumed = overall_progress(100_000_000_000, {
            "transferred_bytes": 2_500_000_000,
            "total_bytes": 2_500_000_000,
            "progress": 100,
            "speed_bps": 10_000_000,
            "eta_seconds": 0,
        }, base_bytes=33_900_000_000)
        self.assertEqual(resumed["transferred_bytes"], 36_400_000_000)
        self.assertEqual(resumed["progress"], 36)


if __name__ == "__main__":
    unittest.main()
