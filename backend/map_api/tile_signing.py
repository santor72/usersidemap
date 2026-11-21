"""Canonical path-prefix HMAC, shared by both raster densities."""

import hashlib
import hmac
import re
from datetime import UTC, datetime

from .tile_auth import reject


def prefix(map_id, format, version):
    return f"/tiles/maps/{map_id}/{format}/{version}"


def signature(secret, path, expiry):
    return hmac.new(secret.encode(), f"{path}\n{expiry}".encode(), hashlib.sha256).hexdigest()


def templates(settings, row, now):
    path = prefix(row["map_id"], row["format"], row["id"])
    extension = "pbf" if row["format"] == "vector" else "webp"
    query = ""
    expires = None
    if settings.signing == "hmac":
        exp = int(now) + settings.signature_ttl
        query = f"?exp={exp}&sig={signature(settings.secret, path, exp)}"
        expires = datetime.fromtimestamp(exp, UTC).isoformat()
    base = settings.public_base + path + "/{z}/{x}/{y}"
    return (
        base + "." + extension + query,
        base + "@2x.webp" + query if row["format"] == "raster" else None,
        expires,
    )


def verify_signature(settings, map_id, format, version, request, now):
    if settings.signing == "none":
        return 31536000
    exp, sig = request.query_params.get("exp", ""), request.query_params.get("sig", "")
    if not re.fullmatch(r"[0-9]{1,12}", exp) or not re.fullmatch(r"[0-9a-f]{64}", sig):
        reject(403, "TILE_SIGNATURE_INVALID", "Tile signature is invalid")
    expected = signature(settings.secret, prefix(map_id, format, version), int(exp))
    if not hmac.compare_digest(sig, expected):
        reject(403, "TILE_SIGNATURE_INVALID", "Tile signature is invalid")
    if int(exp) <= now:
        reject(403, "TILE_SIGNATURE_EXPIRED", "Tile signature expired; refresh catalog")
    return max(0, int(int(exp) - now))
