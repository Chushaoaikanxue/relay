"""Fail the container on a crashed service; Docker restarts a clean process tree.

In particular, a killed nginx master may leave workers holding its port. Merely
restarting the master is insufficient. Supervisor's event protocol uses stdout.
"""
import os
import signal
import sys

while True:
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        break
    headers = dict(field.split(":", 1) for field in line.split())
    payload = sys.stdin.read(int(headers["len"]))
    fields = dict(field.split(":", 1) for field in payload.split())
    sys.stdout.write("RESULT 2\nOK")
    sys.stdout.flush()
    # Supervisor emits STOPPED (not EXITED) for an intentional service stop.
    if fields.get("processname") in {"relay-api", "relay-web"}:
        print("Service crashed; stopping container for a clean restart", file=sys.stderr, flush=True)
        os.kill(os.getppid(), signal.SIGTERM)
