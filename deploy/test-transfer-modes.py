"""Linux integration checks using disposable files and a loopback-only SSH server."""
import hashlib
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from relay_transfer import build_local_command, build_remote_command
from relay_api import RelayApp


def run(command, key=b''):
    result = subprocess.run(['/bin/sh', '-c', command], input=key, capture_output=True, timeout=40)
    assert result.returncode == 0, result.stderr.decode() + result.stdout.decode()
    return result.stdout.decode()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


with tempfile.TemporaryDirectory(prefix='.relay-mode-integration-', dir='/root') as tmp:
    root = Path(tmp)
    source = root / 'source space'
    source.mkdir()
    (source / 'nested').mkdir()
    (source / 'nested' / '你好.txt').write_text('relay test content\n')
    (source / 'empty').mkdir()
    (source / 'large.bin').write_bytes(os.urandom(8 * 1024 * 1024))
    target = root / 'destination'
    target.mkdir()
    task = {'id': str(uuid.uuid4()), 'source_path': str(source), 'destination_path': str(target),
            'bandwidth_limit_kbps': 0, 'delete_enabled': False, 'transfer_mode': 'local'}
    run(build_local_command(task))
    copied = target / source.name
    assert digest(copied / 'large.bin') == digest(source / 'large.bin')
    assert (copied / 'nested' / '你好.txt').read_text() == 'relay test content\n'
    assert (copied / 'empty').is_dir()
    assert run(build_local_command(task, verify=True)) == ''
    (copied / 'nested' / '你好.txt').write_text('mismatch')
    assert run(build_local_command(task, verify=True)).strip(), 'changed content was not detected'
    print('PASS local directory copy and read-only content verification', flush=True)
    single = {**task, 'id': str(uuid.uuid4()), 'source_path': str(source / 'nested' / '你好.txt')}
    run(build_local_command(single))
    assert (target / '你好.txt').is_file()
    print('PASS single-file destination naming', flush=True)

    def interrupt_and_resume(task, build, key=b''):
        task['bandwidth_limit_kbps'] = 512
        process = subprocess.Popen(['/bin/sh', '-c', build(task)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        process.stdin.write(key)
        process.stdin.close()
        pidfile = Path('/tmp') / ('.relay-run-' + task['id']) / 'pid'
        deadline = time.monotonic() + 10
        partial_root = Path(task['destination_path'])
        while time.monotonic() < deadline:
            # Wait for actual payload writing, not only process startup.
            if pidfile.exists() and any(p.is_file() and p.stat().st_size > 128 * 1024 for p in partial_root.rglob('*')):
                break
            if process.poll() is not None:
                raise AssertionError(process.stdout.read().decode())
            time.sleep(.1)
        assert pidfile.exists(), 'missing node-side cancellation handle'
        pgid = int(pidfile.read_text())
        os.killpg(pgid, signal.SIGINT)
        process.wait(timeout=15)
        output = process.stdout.read().decode()
        assert process.returncode != 0, output
        assert not pidfile.exists(), 'process handle not cleaned'
        partials = list(partial_root.rglob('.relay-partial-' + task['id']))
        assert partials and any(p.is_file() for d in partials for p in d.rglob('*')), output
        task['bandwidth_limit_kbps'] = 0
        resumed = run(build(task).replace('--info=progress2', '--info=progress2 --stats'), key)
        matched_line = next(line for line in resumed.splitlines() if line.startswith('Matched data:'))
        matched = int(matched_line.split(':', 1)[1].strip().split()[0].replace(',', ''))
        assert matched > 0, resumed
        assert digest(partial_root / 'large.bin') == digest(source / 'large.bin')
        return matched

    resume_target = root / 'local-resume'
    resume_target.mkdir()
    resume = {**task, 'id': str(uuid.uuid4()), 'source_path': str(source / 'large.bin'), 'destination_path': str(resume_target)}
    matched = interrupt_and_resume(resume, build_local_command)
    print(f'PASS local cancellation and resume: reused {matched} bytes', flush=True)

    keyfile = root / 'client-key'
    hostkey = root / 'host-key'
    for path in (keyfile, hostkey):
        subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)], check=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    authorized = root / 'authorized_keys'
    config = root / 'sshd_config'
    config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {hostkey}
PidFile {root / 'sshd.pid'}
AuthorizedKeysFile {authorized}
StrictModes yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
UsePAM no
LogLevel ERROR
''')
    sshd = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(40):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    break
            except OSError:
                if sshd.poll() is not None:
                    raise AssertionError(sshd.stderr.read().decode())
                time.sleep(.1)
        host_public = ' '.join(Path(str(hostkey) + '.pub').read_text().split()[:2])
        hosts = f'[127.0.0.1]:{port} {host_public}\n'
        private_key = keyfile.read_bytes()
        destination = {'destination_user': 'root', 'destination_transfer_host': '127.0.0.1', 'destination_transfer_port': port}
        for mode in ('public', 'private'):
            dest = root / mode
            dest.mkdir()
            authorized.write_text(f'command="/usr/bin/rrsync -wo {dest}",restrict ' + Path(str(keyfile) + '.pub').read_text())
            authorized.chmod(0o600)
            job = {**task, 'id': str(uuid.uuid4()), 'transfer_mode': mode, 'destination_path': str(dest)}
            build = lambda t, verify=False: build_remote_command(t, destination, hosts, len(private_key), verify=verify)
            run(build(job), private_key)
            assert digest(dest / source.name / 'large.bin') == digest(source / 'large.bin')
            assert run(build(job, verify=True), private_key) == ''
            print(f'PASS {mode} SSH/rrsync transfer and verification', flush=True)
        dest = root / 'remote-resume'
        dest.mkdir()
        authorized.write_text(f'command="/usr/bin/rrsync -wo {dest}",restrict ' + Path(str(keyfile) + '.pub').read_text())
        job = {**task, 'id': str(uuid.uuid4()), 'transfer_mode': 'private', 'source_path': str(source / 'large.bin'), 'destination_path': str(dest)}
        matched = interrupt_and_resume(job, build, private_key)
        print(f'PASS SSH cancellation and resume: reused {matched} bytes', flush=True)

        # Exercise the controller's actual SSH process lifecycle with an isolated database.
        controller_key = root / 'controller-key'
        subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(controller_key)], check=True)
        with authorized.open('a') as file:
            file.write(Path(str(controller_key) + '.pub').read_text())
        controller_dest = root / 'controller-copy'
        controller_dest.mkdir()
        app = RelayApp(str(root / 'controller-state' / 'relay.db'), 'http://127.0.0.1', secure_cookie=False, enable_transfers=False)
        manager = app.transfers
        manager.known_hosts_path.write_text(hosts)
        manager.enabled = True
        task_id = str(uuid.uuid4())
        with app.connect() as db:
            db.execute("INSERT INTO users(id,username,display_name,password_salt,password_hash,role,created_at) VALUES (1,'test','Test',X'00',X'00','admin','2026-09-17')")
            db.execute("INSERT INTO credentials(id,name,fingerprint,key_path,created_at,owner_id) VALUES ('key','test','test',?,'2026-09-17',1)", (str(controller_key),))
            db.execute("INSERT INTO nodes(id,name,status,host,ssh_port,username,credential_id,created_at) VALUES ('node','Test','online','127.0.0.1',?,'root','key','2026-09-17')", (port,))
            db.execute("INSERT INTO tasks(id,name,source,destination,status,owner_id,created_at,updated_at,source_node_id,destination_node_id,source_path,destination_path,transfer_mode,bandwidth_limit_kbps,max_size_bytes,verify_after_transfer) VALUES (?,'Integration','test','test','queued',1,'2026-09-17','2026-09-17','node','node',?,?,'local',512,0,1)", (task_id, str(source / 'large.bin'), str(controller_dest)))
        assert manager.start(task_id)
        pidfile = Path('/tmp') / ('.relay-run-' + task_id) / 'pid'
        deadline = time.monotonic() + 15
        while not pidfile.exists() and time.monotonic() < deadline and manager.is_running(task_id):
            time.sleep(.1)
        assert pidfile.exists(), dict(manager._load_task(task_id))['error_message']
        pgid = int(pidfile.read_text())
        time.sleep(.6)
        with app.connect() as db:
            db.execute("UPDATE tasks SET status='paused',stop_reason='cancel' WHERE id=?", (task_id,))
        manager.cancel(task_id)
        deadline = time.monotonic() + 30
        while manager.is_running(task_id) and time.monotonic() < deadline:
            time.sleep(.1)
        assert not manager.is_running(task_id), 'controller did not finish cancellation'
        assert not pidfile.exists(), 'remote process still running after cancellation acknowledgement'
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError('remote rsync process group survived cancellation')
        with app.connect() as db:
            db.execute("UPDATE tasks SET status='queued',stop_reason=NULL,bandwidth_limit_kbps=NULL WHERE id=?", (task_id,))
        assert manager.start(task_id)
        deadline = time.monotonic() + 20
        while manager.is_running(task_id) and time.monotonic() < deadline:
            time.sleep(.1)
        result = manager._load_task(task_id)
        assert result['status'] == 'completed', (result['status'], result['error_message'], result['log_tail'])
        assert result['verification_status'] == 'passed'
        assert digest(controller_dest / 'large.bin') == digest(source / 'large.bin')
        manager.shutdown()
        print('PASS controller over SSH: cancellation stops remote process; retry and content verification complete', flush=True)
    finally:
        sshd.terminate()
        sshd.wait(timeout=10)
print('ALL TRANSFER INTEGRATION CHECKS PASSED', flush=True)
