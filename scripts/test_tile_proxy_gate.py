"""Explicit Docker Nginx cache integration gate, using only disposable PostGIS."""

import gzip
import json
import subprocess
import threading
import time
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi.testclient import TestClient
from map_api.app import create_app
from test_cable_render_database import (
    published_fixture as published_fixture,  # noqa: PLC0414
)
from test_tile_api import api_fixture as api_fixture  # noqa: PLC0414
from test_tile_api import build_publish, final_folder, headers
from test_vector_pipeline import tile_fixture as tile_fixture  # noqa: PLC0414


def test_hmac_expiry_cannot_be_bypassed_by_proxy_cache(api_fixture, tmp_path):
    conn, dataset, config, token, clock, settings = api_fixture
    report = build_publish(conn, dataset, config)
    manifest = json.loads(
        (final_folder(config, report, "vector") / "manifest.json").read_text()
    )
    entry = manifest["coverage"]["native"]["14"][0]
    settings.update(
        tiles_signing="hmac",
        tiles_hmac_secret="proxy-fixture-key-only-0123456789012345",
        tiles_signature_ttl=10,
    )
    calls = []
    with TestClient(create_app(settings=settings)) as client:
        item = client.get("/api/maps/3/tiles/catalog", headers=headers(token)).json()[
            "formats"
        ]["vector"]["active"]
        parsed = urlsplit(
            item["template"]
            .replace("{z}", "14")
            .replace("{x}", str(entry["x"]))
            .replace("{y}", str(entry["y"]))
        )
        path = parsed.path + "?" + parsed.query

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                return  # No signed query URLs in logs.

            def do_GET(self):
                calls.append(self.path.split("?")[0])
                response = client.get(self.path, headers=dict(self.headers))
                payload = response.content
                if response.headers.get("content-encoding") == "gzip" and payload:
                    payload = gzip.compress(payload, compresslevel=6, mtime=0)
                self.send_response(response.status_code)
                for key, val in response.headers.items():
                    if key.lower() not in (
                        "content-length",
                        "connection",
                        "transfer-encoding",
                    ):
                        self.send_header(key, val)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        origin = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        thread = threading.Thread(target=origin.serve_forever, daemon=True)
        thread.start()
        conf = tmp_path / "nginx.conf"
        conf.write_text(f"""pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events {{}}
http {{
 access_log off;
 proxy_cache_path /tmp/cache keys_zone=tiles:10m;
 server {{ listen 8080;
  location / {{
   proxy_pass http://host.docker.internal:{origin.server_port};
   proxy_cache tiles;
   proxy_cache_revalidate on;
   proxy_cache_valid 200 1h;
   proxy_cache_use_stale off;
   add_header X-Cache-Status $upstream_cache_status always;
  }}
 }}
}}""")
        name = "map-tile-proxy-gate-" + uuid4().hex[:8]
        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    name,
                    "--user",
                    "101:101",
                    "--read-only",
                    "--tmpfs",
                    "/tmp",
                    "--tmpfs",
                    "/var/cache/nginx:uid=101,gid=101,mode=0700",
                    "--cap-drop",
                    "ALL",
                    "-v",
                    str(conf) + ":/etc/nginx/nginx.conf:ro",
                    "-p",
                    "127.0.0.1::8080",
                    "--entrypoint",
                    "nginx",
                    "nginx:1.28-alpine",
                    "-g",
                    "daemon off;",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            port = (
                subprocess.run(
                    ["docker", "port", name, "8080"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                .stdout.strip()
                .split(":")[-1]
            )
            url = "http://127.0.0.1:" + port + path

            def fetch(etag=None):
                try:
                    response = urlopen(
                        Request(url, headers={"If-None-Match": etag} if etag else {}),
                        timeout=5,
                    )
                except HTTPError as error:
                    response = error
                return response.code, response.headers, response.read()

            for _ in range(20):
                try:
                    status, h, payload = fetch()
                    break
                except (URLError, RemoteDisconnected, ConnectionResetError):
                    time.sleep(0.1)
            assert status == 200
            assert gzip.decompress(payload)
            assert (
                "s-maxage=0" in h["Cache-Control"]
                and "must-revalidate" in h["Cache-Control"]
            )
            first = len(calls)
            status, h2, _ = fetch()
            assert status == 200 and len(calls) > first
            assert h2["X-Cache-Status"] != "HIT"
            clock[0] += 11  # No wait: origin clock now rejects the same cached URL.
            status, h3, payload = fetch(h["ETag"])
            assert status == 403
            assert json.loads(payload)["error"]["code"] == "TILE_SIGNATURE_EXPIRED"
            assert h3["Cache-Control"] == "no-store" and len(calls) > first + 1
            audit = {
                "accepted": True,
                "proxy": "nginx:1.28-alpine",
                "configuredCacheTtlSeconds": 3600,
                "sMaxageZeroRespected": True,
                "originRecheckedOnRepeat": True,
                "expiredSignedUrlWithIfNoneMatch": 403,
                "originRequests": len(calls),
                "staleServingDisabled": True,
            }
            Path("reports/tiles/deployment-proxy.json").write_text(
                json.dumps(audit, indent=2) + "\n"
            )
        finally:
            subprocess.run(
                ["docker", "stop", "--time", "1", name],
                capture_output=True,
                check=False,
            )
            origin.shutdown()
            origin.server_close()
            thread.join(timeout=2)
