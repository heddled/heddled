"""Heddled — a self-hostable agent platform where building, debugging, operating
and evaluating an agent are all views on the same event stream.

Public surface (Level 3, the Python API):

    from heddled import Agent, TurnEngine, get_store, submit_message

Everything the console and CLI do goes through the same functions.
"""

__version__ = "0.1.0"

from .events import EVENT_TYPES, Event  # noqa: E402,F401
from .registry import Agent, Registry, Tool, get_registry  # noqa: E402,F401
from .store import Store, get_store  # noqa: E402,F401

__all__ = [
    "Event",
    "EVENT_TYPES",
    "Agent",
    "Tool",
    "Registry",
    "get_registry",
    "Store",
    "get_store",
    "__version__",
]


def __getattr__(name):
    # Lazy re-exports: importing `heddled` must not drag in Flask or providers.
    if name in ("TurnEngine", "TurnResult"):
        from . import engine

        return getattr(engine, name)
    if name in ("submit_message", "start_session", "resolve_approval",
                "inject_operator_message", "fire_trigger", "platform_health"):
        from . import runtime

        return getattr(runtime, name)
    if name == "create_app":
        from .web.app import create_app

        return create_app
    raise AttributeError(name)
