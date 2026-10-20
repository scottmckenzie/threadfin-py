"""Docker HEALTHCHECK: ask the running server for /discover.json on the address and port it uses.

The port comes from /config/settings.json (THREADFIN_PORT and -port are saved there before the server
starts); the address from THREADFIN_BIND, else the bindIpAddress setting, else loopback.
"""

import json
import os
import sys
import urllib.request

try:
    with open("/config/settings.json", encoding="utf-8") as f:
        settings = json.load(f)
except (OSError, ValueError):
    settings = {}

port = settings.get("port") or os.environ.get("THREADFIN_PORT") or "34400"
host = os.environ.get("THREADFIN_BIND") or settings.get("bindIpAddress") or "127.0.0.1"
host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
if ":" in host:
    host = f"[{host}]"

try:
    urllib.request.urlopen(f"http://{host}:{port}/discover.json", timeout=4)
except OSError as exc:
    sys.exit(f"unhealthy: {exc}")
