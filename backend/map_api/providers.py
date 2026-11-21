from dataclasses import dataclass

from map_render.cables import CABLE_SOURCE_KINDS


@dataclass(frozen=True)
class Provider:
    """Server-owned definition of a published feature type."""

    object_type: str
    source_kinds: tuple[str, ...]


PROVIDERS = {
    provider.object_type: provider
    for provider in (
        Provider("cable_line", CABLE_SOURCE_KINDS),
        Provider("node", ("node",)),
        Provider("coupler", ("node",)),
    )
}
