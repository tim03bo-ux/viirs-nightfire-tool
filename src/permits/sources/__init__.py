"""
Source adapters.

Each adapter turns one upstream dataset into normalized `entities` rows. They
all share the same shape:

    read_file(path)      -> DataFrame of raw rows (xlsx/csv/html)
    fetch(...)           -> download the current file, then read_file (optional,
                            needs network access to ERCOT/TCEQ)
    to_entities(df, ...) -> list of dicts ready for db.upsert_entities

Adding a source means writing one module here and registering it in
`permits.pipeline.ADAPTERS`.
"""

from . import base, ercot, tceq_air, tceq_stormwater  # noqa: F401

__all__ = ["base", "ercot", "tceq_air", "tceq_stormwater"]
