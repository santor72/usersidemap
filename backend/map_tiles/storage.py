"""Tile paths backed by files or S3; publication switches a marker, never copies objects."""

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath

from .lifecycle import TileError

RELEASE = re.compile(r"[1-9][0-9]*/(?:vector|raster)/(?:\.staging/)?[0-9a-f-]{36}$")
MARKER = "_release.json"


def md5(data):
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class S3Settings:
    endpoint: str
    bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str = "us-east-1"

    def __post_init__(self):
        if not self.endpoint.startswith(("http://", "https://")) or not all(
            (self.bucket, self.access_key, self.secret_key)
        ):
            raise ValueError("Configure S3 endpoint, bucket and explicit credentials")


@lru_cache(maxsize=4)
def client(settings):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        region_name=settings.region,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=30,
            max_pool_connections=16,
            retries={"mode": "standard", "total_max_attempts": 3},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def root_from_env(env, local):
    backend = env.get("TILES_STORAGE", "filesystem")
    if backend == "filesystem":
        return local
    if backend != "s3":
        raise ValueError("TILES_STORAGE must be filesystem or s3")
    return S3Path(
        S3Settings(
            env.get("TILES_S3_ENDPOINT", ""),
            env.get("TILES_S3_BUCKET", ""),
            env.get("TILES_S3_ACCESS_KEY", ""),
            env.get("TILES_S3_SECRET_KEY", ""),
            env.get("TILES_S3_REGION", "us-east-1"),
        )
    )


@dataclass(frozen=True)
class S3Path:
    settings: S3Settings
    key: str = ""

    def __post_init__(self):
        parts = PurePosixPath(self.key).parts
        if self.key.startswith("/") or any(p in ("..", ".") for p in parts) or "\\" in self.key:
            raise TileError("TILE_PATH_UNSAFE", "Unsafe S3 object key")

    @property
    def object_key(self):
        return self.key.replace("/.staging/", "/")

    def __truediv__(self, other):
        other = str(other)
        if other.startswith("/") or ".." in PurePosixPath(other).parts:
            raise TileError("TILE_PATH_UNSAFE", "Unsafe S3 object key")
        return S3Path(self.settings, "/".join(p for p in (self.key, other) if p))

    @property
    def parent(self):
        parent = str(PurePosixPath(self.key).parent)
        return S3Path(self.settings, "" if parent == "." else parent)

    def resolve(self):
        return self

    def is_relative_to(self, root):
        return self.settings == root.settings and (
            not root.key or self.key == root.key or self.key.startswith(root.key + "/")
        )

    def relative_to(self, root):
        if not self.is_relative_to(root):
            raise ValueError("Path is outside root")
        return PurePosixPath(self.key[len(root.key) :].lstrip("/"))

    def is_symlink(self):
        return False

    def request(self, operation, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return getattr(client(self.settings), operation)(Bucket=self.settings.bucket, **kwargs)
        except (BotoCoreError, ClientError) as error:
            if isinstance(error, ClientError) and error.response["Error"]["Code"] in (
                "404",
                "NoSuchKey",
                "NotFound",
            ):
                raise FileNotFoundError("S3 object is missing") from None
            raise TileError("TILE_STORAGE_FAILED", f"S3 {operation} failed") from None

    def read_bytes(self):
        from botocore.exceptions import BotoCoreError

        response = self.request("get_object", Key=self.object_key)
        body = response["Body"]
        try:
            return body.read()
        except BotoCoreError:
            raise TileError("TILE_STORAGE_FAILED", "S3 download failed") from None
        finally:
            body.close()

    def read_text(self, encoding="utf-8"):
        return self.read_bytes().decode(encoding)

    def write_bytes(self, payload):
        checksum = md5(payload)
        response = self.request(
            "put_object",
            Key=self.object_key,
            Body=payload,
            ContentMD5=base64.b64encode(bytes.fromhex(checksum)).decode(),
            Metadata={"sha256": hashlib.sha256(payload).hexdigest()},
        )
        if response.get("ETag", "").strip('"') != checksum:
            raise TileError("TILE_INVALID", "S3 upload checksum mismatch")
        return len(payload)

    def write_text(self, value, encoding="utf-8"):
        return self.write_bytes(value.encode(encoding))

    def is_file(self):
        try:
            self.request("head_object", Key=self.object_key)
            return True
        except FileNotFoundError:
            return False

    def exists(self):
        if not self.key:
            return True
        if RELEASE.fullmatch(self.key):
            try:
                state = json.loads((self / MARKER).read_text())["state"]
                return state == ("staging" if "/.staging/" in self.key else "final")
            except FileNotFoundError:
                return False
        return self.is_file()

    def is_dir(self):
        return bool(RELEASE.fullmatch(self.key)) and self.exists()

    def mkdir(self, parents=False, exist_ok=False):
        # S3 parents are prefixes; only a release root has a lifecycle marker.
        if not RELEASE.fullmatch(self.key):
            return
        if not exist_ok and (self / MARKER).is_file():
            raise FileExistsError("Release already exists")
        if not (self / MARKER).is_file():
            (self / MARKER).write_text(json.dumps({"state": "staging"}))

    def inventory(self):
        if not RELEASE.fullmatch(self.key):
            raise TileError("TILE_PATH_UNSAFE", "Listing requires a complete release prefix")
        result, token = {}, None
        prefix = self.object_key + "/"
        while True:
            response = self.request(
                "list_objects_v2", Prefix=prefix, **({"ContinuationToken": token} if token else {})
            )
            for item in response.get("Contents", []):
                relative = item["Key"][len(prefix) :]
                if relative != MARKER:
                    result[relative] = item
            if not response.get("IsTruncated"):
                return result
            token = response["NextContinuationToken"]

    def rglob(self, pattern):
        if pattern != "*":
            raise ValueError("Only full release listing is supported")
        return (self / key for key in self.inventory())

    def rename(self, final):
        if (
            not RELEASE.fullmatch(self.key)
            or "/.staging/" not in self.key
            or final.settings != self.settings
            or final.object_key != self.object_key
            or "/.staging/" in final.key
        ):
            raise TileError("TILE_PATH_UNSAFE", "Invalid S3 release promotion")
        (self / MARKER).write_text(json.dumps({"state": "final"}))
        return final

    def remove(self):
        if not RELEASE.fullmatch(self.key):
            raise TileError("TILE_PATH_UNSAFE", "Deletion requires a complete release prefix")
        keys = [self.object_key + "/" + key for key in self.inventory()]
        keys.append(self.object_key + "/" + MARKER)
        for offset in range(0, len(keys), 1000):
            result = self.request(
                "delete_objects",
                Delete={
                    "Objects": [{"Key": key} for key in keys[offset : offset + 1000]],
                    "Quiet": True,
                },
            )
            if result.get("Errors"):
                raise TileError("TILE_STORAGE_FAILED", "S3 release deletion was incomplete")
        if self.inventory() or (self / MARKER).is_file():
            raise TileError("TILE_STORAGE_FAILED", "S3 release deletion was incomplete")
