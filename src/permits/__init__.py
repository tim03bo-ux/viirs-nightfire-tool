"""
permits — TCEQ / ERCOT permit and interconnection intelligence.

Builds a linked database of:
  * ERCOT Generator Interconnection Status (GIS) projects  -> planned generation
  * ERCOT Large Load interconnection requests              -> data centers / crypto / industrial
  * TCEQ air New Source Review permit applications         -> combustion equipment, fuel, emissions
  * TCEQ stormwater construction NOIs (TXR150000)          -> earthwork, months before anything else

and resolves them into *sites*, so a generation project and a data center at the
same location surface as one colocated development.

Entry points:
    from src.permits import pipeline
    pipeline.run(db_path="output/permits.db")

or the CLI:
    python permits_cli.py --help
"""

from . import db, normalize, classify, link, pipeline  # noqa: F401

__all__ = ["db", "normalize", "classify", "link", "pipeline"]
