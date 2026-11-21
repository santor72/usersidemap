#!/usr/bin/env python3
"""External HTTP + actual Compose privilege gates; output never includes credentials/URLs."""

import gzip
import io
import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import dotenv_values
from map_tiles.manifest import decode_vector
from PIL import Image


def main():
    env = dotenv_values(".env")
    base = env["TILES_PUBLIC_BASE_URL"].rstrip("/")
    token = env["NUXT_MAP_TILES_TOKEN"]

    def fetch(path, auth=False, headers=None):
        req = Request(
            path if path.startswith("http") else base + path,
            headers={
                **({"Authorization": "Bearer " + token} if auth else {}),
                **(headers or {}),
            },
        )
        try:
            r = urlopen(req, timeout=10)
        except HTTPError as error:
            r = error
        return r.code, r.headers, r.read()

    assert fetch("/api/maps/3/tiles/catalog")[0] == 401
    assert fetch("/api/maps/4/tiles/catalog", True)[0] == 403
    status, headers, body = fetch("/api/maps/3/tiles/catalog", True)
    assert status == 200
    assert (
        fetch("/api/maps/3/tiles/catalog", True, {"If-None-Match": headers["ETag"]})[0]
        == 304
    )
    catalog = json.loads(body)
    formats = {}
    for format in ("vector", "raster"):
        item = catalog["formats"][format]["active"]
        assert item and not item["tilesBehindActive"]
        version = item["tileVersion"]
        _, _, raw = fetch(item["tilejsonUrl"], True)
        tilejson = json.loads(raw)
        assert tilejson["maxzoom"] == (14 if format == "vector" else 15)
        assert tilejson["displayMaxzoom"] == 18
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import json; from pathlib import Path; print((Path('/tiles/3')/"
                + repr(format)
                + "/"
                + repr(version)
                + "/'manifest.json').read_text())",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        manifest = json.loads(result.stdout)
        tile = manifest["coverage"]["native"][str(tilejson["maxzoom"])][0]
        z, x, y = tilejson["maxzoom"], tile["x"], tile["y"]

        def xyz(template, z=z, x=x, y=y):
            return (
                template.replace("{z}", str(z))
                .replace("{x}", str(x))
                .replace("{y}", str(y))
            )

        status, h, payload = fetch(xyz(item["template"]))
        assert status == 200
        assert (
            fetch(xyz(item["template"]), headers={"If-None-Match": h["ETag"]})[0] == 304
        )
        assert fetch(xyz(item["template"], z=z + 1, x=x * 2, y=y * 2))[0] == 422
        if format == "vector":
            assert h["Content-Encoding"] == "gzip"
            assert h["Content-Type"].startswith("application/vnd.mapbox-vector-tile")
            features = decode_vector(gzip.decompress(payload))
            assert features
            first = features[0]["properties"]
            details_path = f"/api/maps/3/tiles/{version}/objects/{first['sourceKind']}/{first['sourceId']}"
            status, _, details = fetch(details_path, True)
            assert (
                status == 200
                and json.loads(details)["datasetVersion"] == item["datasetVersion"]
            )
            assert fetch(details_path)[0] == 401
            data = {"gzip": True, "decodedFeatures": len(features), "details": True}
        else:
            assert h["Content-Type"].startswith("image/webp")
            assert Image.open(io.BytesIO(payload)).size == (256, 256)
            status, _, twice = fetch(xyz(item["template2x"]))
            assert status == 200 and Image.open(io.BytesIO(twice)).size == (512, 512)
            data = {"webp": True, "density1": [256, 256], "density2": [512, 512]}
        status, _, empty = fetch(xyz(item["template"], z=1, x=0, y=0))
        assert status == 200
        if format == "vector":
            assert not decode_vector(gzip.decompress(empty))
        else:
            assert Image.open(io.BytesIO(empty)).getchannel("A").getextrema() == (0, 0)
        formats[format] = {
            "tileVersion": version,
            "datasetVersion": item["datasetVersion"],
            "nativeMaxzoom": z,
            "emptyTile": True,
            "etag304": True,
            "nativeOverflow422": True,
            **data,
        }
    html = (
        urlopen("http://localhost:" + env.get("FRONTEND_PORT", "3000"), timeout=10)
        .read()
        .decode()
    )
    assert token not in html and "MAP_TILES_VECTOR_DB_PASSWORD" not in html
    assert fetch("/tiles/maps/3")[0] == 404
    assert fetch("/tiles/maps/3/vector/")[0] == 404
    code = """import os,json,psycopg
from pathlib import Path
from entrypoint import database_url
with psycopg.connect(database_url('map_api',os.environ['MAP_API_DB_PASSWORD'])) as c:
 r=c.execute("SELECT current_user,has_table_privilege(current_user,'map_data.features','UPDATE'),has_table_privilege(current_user,'map_data.tile_versions','UPDATE')").fetchone()
 print(json.dumps({'role':r[0],'snapshotWrite':r[1],'tileWrite':r[2],'tilesWritable':os.access(Path('/tiles'),os.W_OK),'ownerPasswordPresent':'MAP_DB_PASSWORD' in os.environ,'sourcePasswordPresent':'US_DB_PASS' in os.environ}))
"""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "python", "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    privileges = json.loads(result.stdout)
    assert privileges == {
        "role": "map_api",
        "snapshotWrite": False,
        "tileWrite": False,
        "tilesWritable": False,
        "ownerPasswordPresent": False,
        "sourcePasswordPresent": False,
    }
    _, _, metrics = fetch("/metrics")
    assert "map_tiles_lag_grace_seconds 210" in metrics.decode()
    assert "map_tiles_redis_up 1" in metrics.decode()
    assert token.encode() not in metrics
    report = {
        "accepted": True,
        "formats": formats,
        "privileges": privileges,
        "tokenAbsentFromHtml": True,
        "listingDenied": True,
        "redisUp": True,
        "lagGraceSeconds": 210,
        "auth401AndMap403": True,
    }
    path = Path("reports/tiles/deployment-api.json")
    path.write_text(json.dumps(report, indent=2) + "\n")
    print("Deployment API accepted; report:", path)


if __name__ == "__main__":
    main()
