"""Effect loader — discovers built-in and user-defined effects."""

import importlib.util
import inspect
from pathlib import Path

# Built-in effects directory (alongside this file)
_BUILTIN_DIR = Path(__file__).parent

# User-defined effects directory (project root / effects/)
_USER_DIR = Path(__file__).resolve().parent.parent.parent / "effects"


def _load_effect_from_file(path: Path) -> dict | None:
    """Load a .py effect file and return its metadata."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    render_fn = getattr(mod, "render", None)
    if render_fn is None:
        return None
    # Tunable param spec for the UI (module-level PARAMS), with defaults filled
    # in from the render() signature so they're not duplicated.
    sig = inspect.signature(render_fn)
    params = []
    for spec in getattr(mod, "PARAMS", []):
        spec = dict(spec)
        spec.setdefault("type", "float")
        spec.setdefault("label", spec["name"])
        if "default" not in spec:
            sig_param = sig.parameters.get(spec["name"])
            if sig_param is not None and sig_param.default is not inspect.Parameter.empty:
                spec["default"] = sig_param.default
        params.append(spec)
    return {
        "name": path.stem,
        "path": str(path),
        "render": render_fn,
        "builtin": _BUILTIN_DIR in path.parents or path.parent == _BUILTIN_DIR,
        "description": (mod.__doc__ or "").strip(),
        # Preferred rendering backend: "smooth" (REST fades) or "streaming".
        # The CLI uses this when no --smooth/--stream flag is given.
        "mode": getattr(mod, "MODE", None),
        "params": params,
    }


def list_effects() -> list[dict]:
    """Return all available effects (built-in + user-defined)."""
    effects = []
    # Built-in effects
    for p in sorted(_BUILTIN_DIR.glob("*.py")):
        if p.name.startswith("_"):
            continue
        eff = _load_effect_from_file(p)
        if eff:
            effects.append(eff)
    # User-defined effects
    if _USER_DIR.exists():
        for p in sorted(_USER_DIR.glob("*.py")):
            if p.name.startswith("_"):
                continue
            eff = _load_effect_from_file(p)
            if eff:
                effects.append(eff)
    return effects


def get_effect(name: str) -> dict:
    """Get a specific effect by name. Raises KeyError if not found."""
    for eff in list_effects():
        if eff["name"] == name:
            return eff
    raise KeyError(f"No effect named '{name}'")
