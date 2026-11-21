"""Assemble FiberMag groups, repairing or dropping failed member geometry."""

from collections import defaultdict

from .geometry import ObjectError, legacy_length, min_zoom


def endpoints(member):
    return (
        ("node", member.get("uzelcode1")),
        ("building", member["building2_id"])
        if (member.get("building2_id") or 0) > 0
        else ("node", member.get("uzelcode2")),
    )


def graph_endpoints(member):
    # Missing IDs do not establish a shared object between unrelated cables.
    return tuple(
        endpoint if endpoint[1] else (f"unknown:{side}", member["code"])
        for side, endpoint in enumerate(endpoints(member))
    )


def bridge_failures(failed, anchors):
    """Walk topology between anchors; never infer adjacency from IDs or proximity."""
    adjacency = defaultdict(list)
    for member_id, member in failed.items():
        for endpoint in graph_endpoints(member):
            adjacency[endpoint].append(member_id)
    visited, bridges = set(), []
    for start in sorted(anchors):
        for first in sorted(adjacency[start]):
            if first in visited:
                continue
            path, current, edge = [], start, first
            while edge not in visited:
                visited.add(edge)
                path.append(edge)
                a, b = graph_endpoints(failed[edge])
                current = b if current == a else a
                if current in anchors:
                    if anchors[start] != anchors[current]:
                        bridges.append(
                            {"memberIds": path, "coordinates": [anchors[start], anchors[current]]}
                        )
                    break
                # An unknown branch or end provides no unambiguous second anchor.
                choices = adjacency[current]
                if len(choices) != 2:
                    break
                edge = next((item for item in choices if item not in visited), edge)
    return bridges


def assemble_group(store, geometry, styles, row, *, error_policy="bridge", on_issue=None):
    if error_policy not in ("bridge", "drop"):
        raise ValueError("Cable group error policy must be bridge or drop")
    member_ids = sorted({r["volscode"] for r in store.children("group_member", row["code"])})
    if not member_ids:
        return None
    members, lines, visible, details, failed, anchors = [], [], [], [], {}, {}
    errors = {}

    def issue(member_id, error):
        errors[member_id] = {"code": error.code, "detail": error.detail}
        if on_issue:
            warning = ObjectError(error.code, error.detail)
            warning.object_kind = "cable"
            warning.object_id = member_id
            on_issue(warning)

    for member_id in member_ids:
        member = store.get("cable", member_id)
        if not member:
            issue(member_id, ObjectError("missing_group_member", "Cable record is absent"))
            details.append({"sourceId": member_id, "status": "dropped"})
            continue
        members.append(member)
        cable_type = store.get("cable_type", member["cable_line_type_id"])
        detail = {
            "sourceId": member_id,
            "cableTypeId": member["cable_line_type_id"],
            "start": dict(zip(("sourceKind", "sourceId"), endpoints(member)[0])),
            "end": dict(zip(("sourceKind", "sourceId"), endpoints(member)[1])),
        }
        details.append(detail)
        if not cable_type:
            issue(member_id, ObjectError("missing_cable_type", "Cable type is absent"))
            detail["status"] = "dropped"
            continue
        if member.get("ishide") != 1 and cable_type.get("is_on_map") == 1:
            visible.append(member)
        # Even failed custom routes may have known endpoints for a straight replacement.
        for kind, endpoint_id in endpoints(member):
            if not endpoint_id:
                continue
            try:
                anchors[(kind, endpoint_id)] = getattr(geometry, kind)(endpoint_id)
            except ObjectError:
                pass
        try:
            coords, source = geometry.cable(member)
        except ObjectError as error:
            issue(member_id, error)
            failed[member_id] = member
            detail["status"] = "dropped"
        else:
            lines.append(coords)
            detail.update(status="original", routeSource=source, lengthM=legacy_length(coords))
            a, b = endpoints(member)
            anchors[a], anchors[b] = coords[0], coords[-1]

    bridges = bridge_failures(failed, anchors) if error_policy == "bridge" else []
    repaired_ids = {member_id for bridge in bridges for member_id in bridge["memberIds"]}
    for bridge in bridges:
        lines.append(bridge["coordinates"])
        bridge["lengthM"] = legacy_length(bridge["coordinates"])
    for detail in details:
        member_id = detail["sourceId"]
        if member_id in repaired_ids:
            detail.update(status="reconstructed", routeSource="straight_bridge")
        if member_id in errors:
            detail["error"] = errors[member_id]
    if not lines:
        if on_issue:
            on_issue(
                ObjectError("empty_group_geometry", "No drawable pieces or recoverable anchors")
            )
        return None
    # One visible member makes the logical group visible; filters never clip its pieces.
    styled_members = visible or [
        m for m in members if store.get("cable_type", m["cable_line_type_id"])
    ]
    representative = styled_members[0]
    length = sum(legacy_length(line) for line in lines)
    return {
        "geometry": {"type": "MultiLineString", "coordinates": lines},
        "length": length,
        "zoom": min_zoom(length),
        "hidden": not visible,
        "planned": all(m.get("isplan") == 1 for m in members),
        "style": styles.cable(representative),
        "layers": sorted({layer for m in members for layer in store.marks("cable", m["code"])}),
        "properties": {
            "routeSource": "cable_group",
            "isBackbone": bool(store.children("mag", representative["code"])),
            "memberIds": member_ids,
            "memberCount": len(member_ids),
            "errorPolicy": error_policy,
            "geometryComplete": not (set(errors) - repaired_ids),
            "reconstructedMemberIds": sorted(repaired_ids),
            "droppedMemberIds": sorted(set(errors) - repaired_ids),
            "bridges": bridges,
            "cableTypeIds": sorted({str(m["cable_line_type_id"]) for m in visible}),
            # Accounting lengths retain all available source records, including dropped pieces.
            "linkLengthM": sum(m.get("link_length") or 0 for m in members) / 100,
            "opticalLengthM": sum(m.get("optical_length") or 0 for m in members) / 100,
            "members": details,
        },
    }


def vertex_count(geom):
    if geom["type"] == "Point":
        return 1
    if geom["type"] == "MultiLineString":
        return sum(len(line) for line in geom["coordinates"])
    return len(geom["coordinates"])
