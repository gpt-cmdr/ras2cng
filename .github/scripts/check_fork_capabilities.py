"""Fail if the fork wheels did not land, so the fork-wheel job cannot quietly test PyPI builds."""

from importlib.metadata import version

from ras2cng import archive_direct, vector_tiles

print("freestiler", version("freestiler"), "terrio", version("terrio"))
capabilities = vector_tiles.freestiler_capabilities()
required = ("arrow", "keep_short_lines", "line_simplify", "layers_deduplicate", "layers_sort_values")
assert all(capabilities[name] for name in required), capabilities
reason = archive_direct.direct_reason()
assert reason is None, reason
