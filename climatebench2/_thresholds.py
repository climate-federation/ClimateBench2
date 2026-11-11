"""Access to the protocol thresholds.

``thresholds.yml`` is the single source of truth for every pass/fail bound in
the protocol (see ``docs/metrics_reference.md``). Diagnostics must read their
bounds through :func:`get_threshold` and never hard-code them.

Layers
------
The thresholds are a **stack of YAML layers**, deep-merged in order. The
default stack is the one packaged ``climatebench2/thresholds.yml``, so
ClimateBench2 itself behaves exactly as before. A protocol built on this
machinery (an AMIP-style comparison, say) pushes its own file on top: it states
only what differs — AMIP gate bounds, new gates, the E_ref mode — and inherits every
scoring constant it does not mention. A mapping in a later layer is merged
key by key into the earlier one; any other value (scalar, list) replaces it.

The stack is process-wide state, set once per run by the CLI
(:func:`set_threshold_layers`) or scoped in a test (:func:`use_thresholds`).
Because it can change after ``climatebench2.diags`` has been imported, **no
threshold may be read at import or class-definition time**: a gate's bounds
are resolved when the gate is evaluated (``pass_fail.GateCheck``).
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

#: The layer every stack starts from unless a caller says otherwise. A layer
#: is a ``(package, resource)`` pair resolved through ``importlib.resources``,
#: a filesystem path, or an already-parsed mapping.
BASE_LAYER: tuple[str, str] = ("climatebench2", "thresholds.yml")

_MISSING: Any = object()

_layers: tuple[Any, ...] = (BASE_LAYER,)
_merged: dict[str, Any] | None = None


def _read_layer(layer: Any) -> dict[str, Any]:  # noqa: ANN401
    """Parse one layer into a mapping."""
    if isinstance(layer, dict):
        return layer
    if isinstance(layer, tuple):
        package, resource = layer
        text = resources.files(package).joinpath(resource).read_text(encoding="utf-8")
    else:
        text = Path(layer).read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    return parsed or {}


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """``overlay`` merged into a deep copy of ``base``.

    Mappings merge key by key; every other value in ``overlay`` (a scalar, a
    list, ``None``) **replaces** the base value outright — a list of bounds is
    one protocol statement, never element-wise mergeable.
    """
    merged = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def threshold_layers() -> tuple[Any, ...]:
    """The active layer stack, lowest first."""
    return _layers


def set_threshold_layers(layers: Sequence[Any]) -> None:
    """Replace the layer stack for the rest of the process.

    ``layers`` are applied in order, so the last one wins. Pass
    ``[BASE_LAYER, ("my_protocol", "thresholds.yml")]`` to overlay a
    protocol on ClimateBench2's values, or a single layer to stand alone.
    """
    global _layers, _merged  # noqa: PLW0603 - process-wide protocol state
    if not layers:
        msg = "At least one threshold layer is required"
        raise ValueError(msg)
    _layers = tuple(layers)
    _merged = None


@contextmanager
def use_thresholds(*overlays: Any, replace: bool = False) -> Iterator[None]:  # noqa: ANN401
    """Scope a layer stack: the active one plus ``overlays``.

    ``replace=True`` uses ``overlays`` alone. The previous stack is restored
    on exit, whatever happens inside.
    """
    saved = _layers
    set_threshold_layers(tuple(overlays) if replace else (*saved, *overlays))
    try:
        yield
    finally:
        set_threshold_layers(saved)


def load_thresholds() -> dict[str, Any]:
    """The merged thresholds of the active layer stack (cached per stack)."""
    global _merged  # noqa: PLW0603 - cache of the process-wide stack
    if _merged is None:
        merged: dict[str, Any] = {}
        for layer in _layers:
            merged = deep_merge(merged, _read_layer(layer))
        _merged = merged
    return _merged


def get_threshold(path: str, default: Any = _MISSING) -> Any:  # noqa: ANN401
    """Return a threshold by dotted path, e.g. ``"tier1.ecs.range"``.

    ``default`` is for **optional protocol switches** only — a key whose
    absence means "ClimateBench2's original behaviour" (``tier2.e_ref.mode``).
    A bound must never be given a default: its absence is a protocol bug.

    Raises
    ------
    KeyError
        If the path does not exist and no ``default`` was given.
    """
    node: Any = load_thresholds()
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            if default is not _MISSING:
                return default
            msg = f"Threshold '{path}' not found in thresholds.yml (missing '{key}')"
            raise KeyError(msg)
        node = node[key]
    return node
