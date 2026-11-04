"""Fail if the fork wheels did not land, so the fork-wheel job cannot quietly test PyPI builds."""

from importlib.metadata import version

from ras2cng import archive_direct, vector_tiles

print("freestiler", version("freestiler"), "terrio", version("terrio"))
capabilities = vector_tiles.freestiler_capabilities()
assert capabilities["arrow"] and capabilities["keep_short_lines"], capabilities
reason = archive_direct.direct_reason()
assert reason is None, reason
