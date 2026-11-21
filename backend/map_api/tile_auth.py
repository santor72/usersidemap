"""Scoped revocable opaque tokens; only digests are persisted."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException


def reject(status, code, message):
    raise HTTPException(
        status, detail={"code": code, "message": message}, headers={"Cache-Control": "no-store"}
    )


def issue_token(conn, name, map_ids, scopes, ttl_seconds):
    if not name or not map_ids or any(type(n) is not int or n <= 0 for n in map_ids):
        raise ValueError("Token requires a name and positive map IDs")
    if not scopes or not set(scopes) <= {"metadata", "details"} or ttl_seconds <= 0:
        raise ValueError("Token requires supported scopes and positive TTL")
    token = secrets.token_urlsafe(32)
    identifier = hashlib.sha256(token.encode()).hexdigest()
    expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    conn.execute(
        """INSERT INTO map_data.api_tokens (id,name,map_ids,scopes,expires_at)
        VALUES (%s,%s,%s,%s,%s)""",
        (identifier, name, sorted(set(map_ids)), sorted(set(scopes)), expires),
    )
    return {
        "id": identifier,
        "token": token,
        "expiresAt": expires.isoformat(),
        "mapIds": map_ids,
        "scopes": scopes,
    }


async def authorize(conn, request, map_id, scope):
    header = request.headers.get("authorization")
    if header is not None:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer":
            reject(401, "AUTH_REQUIRED", "Bearer token required")
    else:
        token = request.cookies.get("map_tiles_session", "")
    if not token or len(token) > 512:
        reject(401, "AUTH_REQUIRED", "Valid metadata token required")
    identifier = hashlib.sha256(token.encode()).hexdigest()
    row = await (
        await conn.execute(
            """SELECT map_ids,scopes FROM map_data.api_tokens
        WHERE id=%s AND revoked_at IS NULL AND expires_at>clock_timestamp()""",
            (identifier,),
        )
    ).fetchone()
    if not row:
        reject(401, "AUTH_REQUIRED", "Token is invalid, revoked or expired")
    if map_id not in row["map_ids"] or scope not in row["scopes"]:
        reject(403, "ACCESS_DENIED", "Token scope does not allow this request")
