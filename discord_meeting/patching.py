"""Find the classes the *running* gateway actually uses.

hermes imports directory-based plugins under its own namespace
(``hermes_plugins.<slug>``, see hermes_cli/plugins.py:_NS_PARENT), so the
Discord adapter the gateway is running is
``hermes_plugins.discord_platform.adapter.DiscordAdapter``. Importing
``plugins.platforms.discord.adapter`` — the same file by its on-disk path —
produces a *second, unrelated* class object. Patching that one succeeds
silently and changes nothing at runtime, which is exactly how auto-join came
out dead on arrival.

So: never import a fresh copy. Scan ``sys.modules`` for what is already
loaded and patch every match, whatever namespace it arrived under.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)


def live_attr(attr: str, module_suffixes: Tuple[str, ...]) -> List[Tuple[Any, Any]]:
    """Return ``(module, value)`` for every loaded module exposing *attr*.

    Matching is on the module name's tail so both the namespaced
    (``hermes_plugins.discord_platform.adapter``) and path-style
    (``plugins.platforms.discord.adapter``) forms are covered — if both happen
    to be loaded, both get patched, since which one a given call site holds is
    not knowable from here.
    """
    found: List[Tuple[Any, Any]] = []
    for name, module in list(sys.modules.items()):
        if module is None or not name.endswith(module_suffixes):
            continue
        value = getattr(module, attr, None)
        if value is not None:
            found.append((module, value))
    if not found:
        logger.warning(
            "discord_meeting: %s not found in loaded modules %s — patch skipped",
            attr, module_suffixes,
        )
    return found


DISCORD_ADAPTER_MODULES = (
    "discord_platform.adapter",
    "platforms.discord.adapter",
)
