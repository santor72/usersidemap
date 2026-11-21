# Partner network lookup

[Integration guide in Russian](partner-network-api-guide.ru.md)

`POST /api/network/nearby` finds cables, nodes and couplers near a WGS84 point.
The partner geocodes the address before calling this endpoint. Use the result
to screen requests for engineering review. The distance measures proximity.
Connection feasibility also depends on available capacity and a viable route
to the building.

## Access

Send the token as `Authorization: Bearer <token>`. Its `network:nearby` scope
requires exactly one permitted `mapId`. The server uses the active published
dataset for that map and the configured source. The request body contains only
the coordinates and radius. Serve the partner endpoint over HTTPS.

After applying migrations, an administrator can issue a time-limited test token
for map 3. The command prints the plaintext token once.

```sh
docker compose --profile tools run --rm jobs tokens create \
  --name partner-nearby-test --map 3 --scope network:nearby --ttl-seconds 86400
```

For a direct Python deployment, run
`uv run --project backend map-token create --name partner-nearby-test --map 3 --scope network:nearby --ttl-seconds 86400`
with `MAP_TOKEN_ADMIN_DATABASE_URL` set. The runtime role needs `SELECT` on
`map_data.api_tokens` and the same snapshot permissions as the map API.
Use `tokens revoke --id <id>` to revoke a token in Compose.

## Request and response

```sh
curl -fsS "${MAP_API_URL}/api/network/nearby" \
  -H "Authorization: Bearer ${PARTNER_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{"latitude":55.003,"longitude":37.003,"radiusM":300}'
```

Latitude and longitude are finite numbers within ±90 and ±180 degrees.
`radiusM` is a finite non-negative number in metres. Set it to `0` to search
the whole permitted map for the nearest objects. The response uses
`searchMode: "nearest"` for that request, or `empty` if there are no eligible
objects. The examples below use the same version to show how mode and results
change.

```json
{
  "mapId": 3,
  "datasetVersion": "52fcd822-2828-4594-8cd6-32ed4f3710c9",
  "radiusM": 300,
  "searchMode": "within_radius",
  "cables": [{"objectId": "cable:10", "distanceM": 42.5}],
  "nodes": [],
  "couplers": [{
    "objectId": "node:2",
    "distanceM": 85,
    "connectedCables": [{"cableId": "cable:10"}]
  }],
  "alternatives": {
    "nodes": [{"objectId": "node:3", "distanceM": 310, "withinRadius": false, "connectedCables": []}],
    "couplers": [{
      "objectId": "node:4",
      "distanceM": 410,
      "withinRadius": false,
      "connectedCables": [{"cableId": "cable:27", "groupId": "cable_group:100"}]
    }]
  }
}
```

```json
{
  "mapId": 3,
  "datasetVersion": "52fcd822-2828-4594-8cd6-32ed4f3710c9",
  "radiusM": 300,
  "searchMode": "nearest_outside_radius",
  "cables": [
    {"objectId": "cable_group:100", "distanceM": 420},
    {"objectId": "cable:10", "distanceM": 605}
  ],
  "nodes": [{"objectId": "node:3", "distanceM": 950, "connectedCables": []}],
  "couplers": [
    {"objectId": "node:2", "distanceM": 610, "connectedCables": [{"cableId": "cable:10"}]},
    {
      "objectId": "node:4",
      "distanceM": 730,
      "connectedCables": [{"cableId": "cable:27", "groupId": "cable_group:100"}]
    }
  ],
  "alternatives": {"nodes": [], "couplers": []}
}
```

Each main list contains up to three objects sorted by distance, with deterministic
ordering for ties. The radius includes its boundary. If any object is within
the radius, the main lists contain only in-radius objects. If the radius is empty
across all types, all eligible objects in the same map can be considered for
the fallback. If the published dataset has no eligible objects, the mode is
`empty` and all lists are empty.

`alternatives.nodes` and `alternatives.couplers` contain up to two additional
objects of each type, ordered by distance. An alternative cannot also appear
in a main list. Its `withinRadius` flag shows whether it falls inside the
requested radius. All results refer to the same map and dataset version.
With `radiusM: 0`, `withinRadius` is true only for alternatives at a reported
distance of zero.

Every node and coupler contains `connectedCables`, even if the list is empty.
The list comes from cable endpoints recorded in Userside. It can include
cables outside the requested radius or outside the main cable list. For
a grouped cable, `cableId` identifies the segment and `groupId` identifies
the parent group. If a segment belongs to multiple eligible groups, it can
appear once for each group. The list excludes dropped group segments and
honours the allowed cable types. For snapshots imported before segment-level
visibility and planning flags were stored, group segments inherit those
flags from the group. A fresh sync enables segment-level filtering.

The automated PostGIS fixture provides two reproducible smoke cases on map 3.
With the test dataset loaded, latitude `55.003`, longitude `37.003`, and
radius `0.01` m return only `cable:10` in the main lists, at distance zero,
with nodes and couplers in `alternatives`. Latitude `54`,
longitude `37`, and radius `300` m trigger the fallback and return
`cable:10`, with nodes and couplers also outside the radius.

The service measures cable distance to the closest point on the full line or
multiline, and node and coupler distance to their stored points. It excludes
hidden and planned objects. Set `MAP_API_NETWORK_CABLE_TYPE_IDS` to a
comma-separated list of eligible cable type IDs (default `1`). A cable group
qualifies if it matches the allowlist and counts as one object. Map zoom and
display layers have no effect on the lookup.

Errors use `{"error":{"code":"…","message":"…"}}`. The API returns `401` for
missing, invalid, expired or revoked credentials, `403` for missing scope,
`422` for invalid input, and `503` when published data or the database is
unavailable. The request and response schemas are available in `/openapi.json`.
