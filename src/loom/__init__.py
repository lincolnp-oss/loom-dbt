"""Metadata-driven dbt authoring: generation, validation and discovery."""

from importlib.metadata import PackageNotFoundError, version

from loom.errors import LoomError
from loom.inventory import (
    InventoryCoverage,
    InventoryEntry,
    check_inventory_coverage,
    search_inventory,
    validate_project,
)

try:
    __version__ = version("loom-dbt")
except PackageNotFoundError:  # source tree without an installed distribution
    __version__ = "unknown"

__all__ = [
    "InventoryCoverage",
    "InventoryEntry",
    "LoomError",
    "__version__",
    "check_inventory_coverage",
    "search_inventory",
    "validate_project",
]
