"""Staging integrity verification independent of the PostGIS encoder."""

import hashlib
import json
from pathlib import Path

from .geometry import EXTENT, tile_bounds
from .lifecycle import TileError
from .storage import S3Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    options = {"separators": (",", ":")} if isinstance(path, S3Path) else {"indent": 2}
    path.write_text(json.dumps(value, sort_keys=True, **options) + "\n", encoding="utf-8")


def decode_vector(payload):
    import mapbox_vector_tile

    try:
        layers = mapbox_vector_tile.decode(payload)
    except Exception as error:
        raise TileError("TILE_INVALID", "MVT failed independent decoding") from error
    if set(layers) != {"cable_line"} or layers["cable_line"]["extent"] != EXTENT:
        raise TileError("TILE_INVALID", "Unexpected MVT layer or extent")
    features = layers["cable_line"]["features"]
    for feature in features:
        props = feature["properties"]
        if feature["geometry"]["type"] not in ("LineString", "MultiLineString"):
            raise TileError("TILE_INVALID", "MVT cable is not a line")
        required = {
            "objectId",
            "sourceKind",
            "sourceId",
            "layerIds",
            "__mapColor",
            "__mapWidth",
            "__mapDashArray",
            "renderOrder",
        }
        if (
            not required <= props.keys()
            or not props["layerIds"].startswith(",")
            or not props["layerIds"].endswith(",")
        ):
            raise TileError("TILE_INVALID", "MVT attributes do not match the cable contract")
    return features


def validate_staging(folder: Path, manifest, check=lambda: None):
    """Verify every planned tile, bytes/checksum, decode and exact input IDs."""
    expected = {"manifest.json", "report.json"}
    count = 0
    inventory = folder.inventory() if isinstance(folder, S3Path) else None
    for zoom, entries in manifest["coverage"]["native"].items():
        z = int(zoom)
        if not manifest["minzoom"] <= z <= manifest["maxzoom"]:
            raise TileError("MANIFEST_INVALID", "Coverage zoom outside native range")
        for entry in entries:
            if count % 128 == 0:
                check()
            x, y = entry["x"], entry["y"]
            tile_bounds(z, x, y)
            if entry["status"] != "prebaked":
                raise TileError("MANIFEST_INVALID", "Native tile must be prebaked")
            if manifest["format"] == "vector":
                records = [(entry, f"{z}/{x}/{y}.pbf", None)]
            elif manifest["format"] == "raster":
                if set(entry["files"]) != {"1", "2"} or manifest.get("densities") != [1, 2]:
                    raise TileError("MANIFEST_INVALID", "Raster must contain both densities")
                records = [
                    (entry["files"][str(d)], f"{z}/{x}/{y}{'@2x' if d == 2 else ''}.webp", d)
                    for d in (1, 2)
                ]
            else:
                raise TileError("MANIFEST_INVALID", "Unknown tile format")
            for record, path, density in records:
                if path in expected or record["path"] != path:
                    raise TileError("MANIFEST_INVALID", "Duplicate or unsafe tile record")
                expected.add(path)
                target = folder / path
                if inventory is not None and record.get("md5"):
                    stored = inventory.get(path)
                    if not stored:
                        raise TileError("TILE_FILE_MISSING", "Planned S3 tile is missing")
                    if (
                        stored["Size"] != record["bytes"]
                        or stored["ETag"].strip('"') != record["md5"]
                    ):
                        raise TileError("TILE_INVALID", "Stored S3 checksum mismatch")
                    # Independently decoded before PUT; ContentMD5 and returned ETag
                    # bind those bytes to the immutable remote object.
                    count += 1
                    continue
                if target.is_symlink() or not target.is_file():
                    raise TileError("TILE_FILE_MISSING", "Planned tile file is missing")
                payload = target.read_bytes()
                if len(payload) != record["bytes"] or digest(payload) != record["sha256"]:
                    raise TileError("TILE_INVALID", "Tile file checksum mismatch")
                if density is None:
                    features = decode_vector(payload)
                    actual = [f["properties"]["objectId"] for f in features]
                    if len(actual) != len(set(actual)) or sorted(actual) != entry["objectIds"]:
                        raise TileError(
                            "TILE_INVALID", "Decoded MVT IDs differ from selected inputs"
                        )
                else:
                    validate_webp(payload, density)
                count += 1
    if set(manifest["coverage"]["native"]) != {
        str(z) for z in range(manifest["minzoom"], manifest["maxzoom"] + 1)
    }:
        raise TileError("MANIFEST_INVALID", "Native zoom coverage is incomplete")
    actual_files = (
        set(inventory)
        if inventory is not None
        else {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    )
    if actual_files != expected or (
        inventory is None and any(p.is_symlink() for p in folder.rglob("*"))
    ):
        raise TileError("MANIFEST_INVALID", "Staging contains unexpected files or symlinks")
    if json.loads((folder / "manifest.json").read_text()) != manifest:
        raise TileError("MANIFEST_INVALID", "Disk manifest differs from validated manifest")
    check()
    return count


def validate_webp(payload, density):
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != "WEBP" or image.size != (256 * density, 256 * density):
                raise TileError("TILE_INVALID", "Invalid raster format or density dimensions")
            image.load()
    except (OSError, ValueError) as error:
        raise TileError("TILE_INVALID", "WebP failed independent decoding") from error
