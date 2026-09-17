"""The raw layer: ERCOT source files to typed Arrow tables, one table per source file.

Parsers (``psse``, ``crr``, ``dam``) are pure functions of bytes: they rename columns
to snake_case and set types, and change nothing else. No joins, no unit conversions,
no dropped rows. Every parser checks the structure it expects (headers, field counts,
section order) and raises ``ParseError`` naming what differs, so a format change at
ERCOT fails loudly instead of producing quietly wrong tables. ``build`` writes the
parsed tables to Parquet with provenance.
"""

from .table import ParseError, snake_case

__all__ = ["ParseError", "snake_case"]
