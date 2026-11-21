import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from map_tiles.storage import root_from_env


@dataclass(frozen=True)
class TileSettings:
    root: Path
    source: str = "userside"
    public_base: str = "http://localhost:8000"
    signing: str = "none"
    secret: str = ""
    signature_ttl: int = 86400
    status_ttl: int = 30
    redis_url: str | None = None
    lag_policy: str = "banner"
    catalog_poll_seconds: int = 0
    lag_grace_seconds: int = 210

    def __post_init__(self):
        if self.signing not in ("none", "hmac") or self.lag_policy != "banner":
            raise ValueError("Use signing none|hmac and lag policy banner")
        if self.signing == "hmac" and len(self.secret) < 32:
            raise ValueError("HMAC secret must have at least 32 characters")
        if (
            min(self.signature_ttl, self.status_ttl) <= 0
            or min(self.catalog_poll_seconds, self.lag_grace_seconds) < 0
        ):
            raise ValueError("Invalid tile TTL/poll settings")
        if (
            not self.public_base.startswith(("https://", "http://"))
            or "?" in self.public_base
            or "#" in self.public_base
        ):
            raise ValueError("Configure a trusted absolute public API base URL")

    @classmethod
    def load(cls, settings):
        env = {**dotenv_values(Path.cwd() / ".env"), **os.environ}

        def get(key, name, default):
            return settings.get(key, env.get(name) or default)

        return cls(
            root_from_env(
                {
                    **env,
                    **(
                        {"TILES_STORAGE": settings.get("tiles_storage", "filesystem")}
                        if "tiles_root" in settings
                        else {}
                    ),
                },
                Path(get("tiles_root", "TILES_ROOT", "./data/tiles")).resolve(),
            ),
            get("source_system", "MAP_API_SOURCE_SYSTEM", "userside"),
            get("tiles_public_base", "TILES_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
            get("tiles_signing", "TILES_XYZ_SIGNING", "none"),
            get("tiles_hmac_secret", "TILES_HMAC_SECRET", ""),
            int(get("tiles_signature_ttl", "TILES_SIGNATURE_TTL_SECONDS", 86400)),
            int(get("tiles_status_ttl", "TILES_VERSION_STATUS_TTL_SECONDS", 30)),
            get("redis_url", "MAP_REDIS_URL", None),
            get("tiles_lag", "TILES_LAG", "banner"),
            int(get("tiles_catalog_poll_seconds", "TILES_CATALOG_POLL_SECONDS", 0)),
            int(get("tiles_lag_grace_seconds", "TILES_LAG_GRACE_SECONDS", 210)),
        )
