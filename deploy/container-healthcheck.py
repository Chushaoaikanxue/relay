"""Check both the static frontend and proxied API, not merely live processes."""
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/relay/", timeout=3) as response:
    if b'<div id="root">' not in response.read():
        raise SystemExit("Frontend health check failed")
with urllib.request.urlopen("http://127.0.0.1:8080/relay/api/health", timeout=3) as response:
    if json.load(response).get("ok") is not True:
        raise SystemExit("API health check failed")
