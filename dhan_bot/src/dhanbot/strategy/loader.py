"""Load a strategy by name (``ema_cross``), dotted module (``mypkg.strats.foo``) or file
(``my_strats/foo.py:MyStrategy``, relative to the config file)."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .base import Strategy


class StrategyLoadError(Exception):
    pass


def _pick(module: ModuleType, class_name: str | None) -> type[Strategy]:
    if class_name:
        cls = getattr(module, class_name, None)
        if not (inspect.isclass(cls) and issubclass(cls, Strategy)):
            raise StrategyLoadError(f"{module.__name__} has no Strategy subclass {class_name!r}")
        return cls
    found = [c for _, c in inspect.getmembers(module, inspect.isclass)
             if issubclass(c, Strategy) and c is not Strategy and c.__module__ == module.__name__]
    if len(found) != 1:
        raise StrategyLoadError(f"{module.__name__}: expected exactly one Strategy subclass, found "
                                f"{[c.__name__ for c in found]} - use 'module:ClassName'")
    return found[0]


def load_strategy(spec: str, params: dict[str, Any] | None = None, base_dir: str | Path = ".") -> Strategy:
    if not spec:
        raise StrategyLoadError("no strategy configured")
    path_part, _, class_name = spec.partition(":")
    if path_part.endswith(".py"):
        path = Path(path_part)
        if not path.is_absolute():
            path = Path(base_dir) / path
        if not path.exists():
            raise StrategyLoadError(f"strategy file not found: {path}")
        mod_name = f"dhanbot_user_strategy_{path.stem}"
        spec_obj = importlib.util.spec_from_file_location(mod_name, path)
        if spec_obj is None or spec_obj.loader is None:
            raise StrategyLoadError(f"cannot import {path}")
        module = importlib.util.module_from_spec(spec_obj)
        sys.modules[mod_name] = module
        spec_obj.loader.exec_module(module)
    else:
        module = None
        errors = []
        for candidate in (f"dhanbot.strategy.{path_part}", f"dhanbot.strategy.examples.{path_part}", path_part):
            try:
                module = importlib.import_module(candidate)
                break
            except ModuleNotFoundError as e:
                if e.name not in candidate:  # the module exists but one of *its* imports failed
                    raise
                errors.append(candidate)
        if module is None:
            raise StrategyLoadError(f"strategy {spec!r} not found (tried {errors})")
    cls = _pick(module, class_name or None)
    try:
        return cls(params or {})
    except ValueError as e:
        raise StrategyLoadError(str(e)) from e
