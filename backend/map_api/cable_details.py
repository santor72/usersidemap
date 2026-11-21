from collections import Counter


def endpoint_refs(feature):
    if "members" in feature["properties"]:
        degree = Counter()
        for member in feature["properties"]["members"]:
            if member["status"] == "dropped":
                continue
            for side in ("start", "end"):
                endpoint = member[side]
                if endpoint["sourceId"]:
                    degree[endpoint["sourceKind"], endpoint["sourceId"]] += 1
        # A closed group has no terminal; show the objects on its loop.
        return [ref for ref, count in degree.items() if count == 1] or list(degree)

    links = {link["role"]: (link["targetKind"], link["targetId"]) for link in feature["links"]}
    return [links.get(role) for role in ("start", "end")]


async def cable_endpoint_names(conn, dataset_id, feature):
    refs = endpoint_refs(feature)
    titles = feature["properties"].get("endpointTitles", {}).copy()
    keys = [f"{ref[0]}:{ref[1]}" if ref else None for ref in refs]
    missing = {ref[1] for ref, key in zip(refs, keys) if ref and key not in titles}
    if missing:
        # Older snapshots still have titles for endpoints published as map features.
        rows = await (
            await conn.execute(
                """SELECT source_kind,source_id,title FROM map_data.features
                WHERE dataset_id=%s AND source_kind IN ('node','building') AND source_id=ANY(%s)""",
                (dataset_id, list(missing)),
            )
        ).fetchall()
        for row in rows:
            titles.setdefault(f"{row['source_kind']}:{row['source_id']}", row["title"].strip())
    return [titles.get(key) or None for key in keys]
