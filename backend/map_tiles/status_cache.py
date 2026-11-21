"""Bounded Redis status cache; PostgreSQL remains authoritative.

Invalidate after COMMIT on publish/rollback/retire/delete. A concurrent miss
may refill an old value after invalidation: its lifetime is bounded by SET TTL.
Missing expected files must trigger a forced DB lookup in the XYZ handler.
"""

import json
from uuid import UUID

import redis
import redis.asyncio as async_redis
from redis.asyncio.retry import Retry as AsyncRetry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

STATUS_SQL = """SELECT id,source_system,map_id,format,dataset_version,status,final_path
    FROM map_data.tile_versions WHERE id=%s"""
STATUSES = {"building", "validated", "ready", "failed", "retired", "deleted"}


def status_key(version):
    return f"map:tiles:status:v1:{UUID(str(version))}"


def normalize(row, version):
    if row is None:
        return None
    record = json.loads(json.dumps(row, default=str))
    if not isinstance(record, dict):
        raise TypeError("Invalid cached tile status")
    version = str(UUID(str(version)))
    if (
        record.get("id") != version
        or record.get("status") not in STATUSES
        or record.get("format") not in ("vector", "raster")
        or type(record.get("map_id")) is not int
        or record["map_id"] <= 0
        or not isinstance(record.get("source_system"), str)
        or not record["source_system"]
        or record.get("final_path") != f"{record['map_id']}/{record['format']}/{version}"
    ):
        raise ValueError("Invalid cached tile status")
    UUID(record["dataset_version"])
    return record


class VersionStatusCache:
    def __init__(self, client=None, *, ttl=30):
        if type(ttl) is not int or ttl <= 0:
            raise ValueError("Redis status TTL must be positive")
        self.client, self.ttl = client, ttl
        self.hits = self.misses = self.errors = 0

    @classmethod
    def from_url(cls, url, *, ttl=30):
        client = (
            redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
                retry=Retry(NoBackoff(), 0),
            )
            if url
            else None
        )
        return cls(client, ttl=ttl)

    def resolve(self, conn, version, *, force=False):
        key = status_key(version)
        if self.client and not force:
            try:
                cached = self.client.get(key)
                if cached is not None:
                    record = normalize(json.loads(cached), version)
                    if record is not None:
                        self.hits += 1
                        return record
            except (RedisError, ValueError, TypeError, KeyError):
                self.errors += 1
        self.misses += 1
        record = normalize(conn.execute(STATUS_SQL, (version,)).fetchone(), version)
        if self.client:
            try:
                if record is None:
                    self.client.delete(key)
                else:
                    self.client.set(key, json.dumps(record), ex=self.ttl)
            except RedisError:
                self.errors += 1
        return record

    def invalidate(self, version):
        if self.client:
            try:
                self.client.delete(status_key(version))
            except RedisError:
                self.errors += 1

    def close(self):
        if self.client:
            self.client.close()


class AsyncVersionStatusCache(VersionStatusCache):
    @classmethod
    def from_url(cls, url, *, ttl=30):
        client = (
            async_redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
                retry=AsyncRetry(NoBackoff(), 0),
            )
            if url
            else None
        )
        return cls(client, ttl=ttl)

    async def resolve(self, conn, version, *, force=False, fetch=None):
        key = status_key(version)
        if self.client and not force:
            try:
                cached = await self.client.get(key)
                if cached is not None:
                    record = normalize(json.loads(cached), version)
                    if record is not None:
                        self.hits += 1
                        return record
            except (RedisError, ValueError, TypeError, KeyError):
                self.errors += 1
        self.misses += 1
        row = (
            await fetch(STATUS_SQL, (version,))
            if fetch
            else await (await conn.execute(STATUS_SQL, (version,))).fetchone()
        )
        record = normalize(row, version)
        if self.client:
            try:
                if record is None:
                    await self.client.delete(key)
                else:
                    await self.client.set(key, json.dumps(record), ex=self.ttl)
            except RedisError:
                self.errors += 1
        return record

    async def invalidate(self, version):
        if self.client:
            try:
                await self.client.delete(status_key(version))
            except RedisError:
                self.errors += 1

    async def close(self):
        if self.client:
            await self.client.aclose()
