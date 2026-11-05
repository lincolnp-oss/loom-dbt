from __future__ import annotations

from loom.dialects import get_dialect
from loom.errors import LoomError

from loom.renderers.base import BaseRenderer
from loom.renderers.bronze import BronzeRenderer
from loom.renderers.dimension import DimensionRenderer
from loom.renderers.fact import FactRenderer
from loom.renderers.gold import GoldRenderer
from loom.renderers.hub import HubRenderer
from loom.renderers.link import LinkRenderer
from loom.renderers.mart import MartRenderer
from loom.renderers.normalized_table import NormalizedTableRenderer
from loom.renderers.bridge import BridgeRenderer
from loom.renderers.obt import OBTRenderer
from loom.renderers.pit import PitRenderer
from loom.renderers.reference import ReferenceHubRenderer, ReferenceTableRenderer
from loom.renderers.satellite import SatelliteRenderer
from loom.renderers.scd2_dimension import SCD2Renderer
from loom.renderers.silver import SilverRenderer
from loom.renderers.staging import StagingRenderer
from loom.renderers.view import ViewRenderer


RENDERERS: dict[str, type[BaseRenderer]] = {
    "staging": StagingRenderer,
    "hub": HubRenderer,
    "link": LinkRenderer,
    "scd2_dimension": SCD2Renderer,
    "fact": FactRenderer,
    "dimension": DimensionRenderer,
    "bronze": BronzeRenderer,
    "silver": SilverRenderer,
    "gold": GoldRenderer,
    "obt": OBTRenderer,
    "normalized_table": NormalizedTableRenderer,
    "view": ViewRenderer,
    "mart": MartRenderer,
    "satellite": SatelliteRenderer,
    "pit": PitRenderer,
    "reference_hub": ReferenceHubRenderer,
    "reference_table": ReferenceTableRenderer,
    "bridge": BridgeRenderer,
}


def get_renderer(generator: str, dialect_name: str) -> BaseRenderer:
    """Get the renderer for a generator and dialect."""
    if generator not in RENDERERS:
        raise LoomError(f"unknown generator {generator!r}")
    dialect = get_dialect(dialect_name)
    return RENDERERS[generator](dialect)
