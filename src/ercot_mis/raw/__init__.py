"""The raw layer: each ERCOT source file as one typed Arrow table.

A parser is a function of bytes. It renames columns to snake_case, sets types and changes
nothing else: no joins, no unit conversions, no dropped rows. It checks the structure it
expects (headers, field counts, section order) and raises ``ParseError`` naming the
difference. ``build`` writes the tables to Parquet with the identity of their source.
"""

from .table import ParseError, snake_case

__all__ = ["ParseError", "snake_case"]
