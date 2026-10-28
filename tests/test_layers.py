"""The package reads in one direction: each layer imports only from the layers before it."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "ercot_mis"

# Earliest first. A module may import from its own layer and any earlier one, never a later one.
LAYERS = [
    {"products", "config", "clock"},  # vocabulary: no dependencies
    {"sources"},                      # how bytes arrive
    {"archive"},                      # layer 0: original documents
    {"raw"},                          # layer 1: typed tables, one per source file
    {"core"},                         # layer 2: tidy keyed tables shared by CRR and DAM
    {"out"},                          # layer 3: what consumers read
    {"checks"},                       # layer 4: the layers tested against ERCOT's results
    {"viewer"},
    {"session", "__init__"},          # strings the layers together; the only writer
]


def layer_of(name: str) -> int:
    return next(i for i, names in enumerate(LAYERS) if name in names)


def imported(path: Path) -> set[str]:
    """Top-level ercot_mis names a module imports, relative or absolute, at any depth."""
    parts = path.relative_to(PACKAGE).with_suffix("").parts
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative: climb from the module's own package, one level per extra dot
                package = list(parts[:-1])
                target = package[: len(package) - (node.level - 1)] + (node.module.split(".") if node.module else [])
                names |= {target[0]} if target else {a.name for a in node.names if not a.name.startswith("__")}  # __version__ is metadata
            elif node.module and node.module.startswith("ercot_mis."):
                names.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            names |= {a.name.split(".")[1] for a in node.names if a.name.startswith("ercot_mis.")}
    return names


def test_no_module_imports_a_later_layer():
    backwards = []
    for path in PACKAGE.rglob("*.py"):
        own = path.relative_to(PACKAGE).parts[0].removesuffix(".py")
        for name in imported(path):
            if layer_of(name) > layer_of(own):
                backwards.append(f"{path.relative_to(PACKAGE)} imports {name}")
    assert not backwards, backwards
