"""A mapping keyed by object identity, for marks on one queued copy of a song.

The user queue may hold the same song more than once, and two queued
entries can even be the same object.  Listener request bridges and the
clockwheel step a request fills belong to one queued item, never to every
item with the same path, so they are keyed by the item itself.  The map
keeps each key alive, so an ``id`` is never reused while it is a key.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, overload


class IdentityMap[K, V]:
    """``{key: value}`` where keys match by ``is``, not ``==``."""

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: dict[int, tuple[K, V]] = {}

    @overload
    def get(self, key: object) -> V | None: ...
    @overload
    def get[D](self, key: object, default: D) -> V | D: ...
    def get(self, key: object, default: object = None) -> object:
        """The value for *key* itself, or *default*."""
        item = self._items.get(id(key))
        return item[1] if item is not None and item[0] is key else default

    def __setitem__(self, key: K, value: V) -> None:
        self._items[id(key)] = (key, value)

    def __contains__(self, key: object) -> bool:
        item = self._items.get(id(key))
        return item is not None and item[0] is key

    def pop(self, key: object, default: V | None = None) -> V | None:
        """Remove *key* and return its value, or *default* when it is not a key."""
        item = self._items.get(id(key))
        if item is None or item[0] is not key:
            return default
        del self._items[id(key)]
        return item[1]

    def items(self) -> Iterator[tuple[K, V]]:
        """``(key, value)`` pairs, a copy safe to change the map under."""
        return iter(list(self._items.values()))

    def retain(self, alive: Iterable[object]) -> None:
        """Forget every key that is not one of the objects in *alive*."""
        keep = {id(obj) for obj in alive}
        for ident in [ident for ident in self._items if ident not in keep]:
            del self._items[ident]

    def clear(self) -> None:
        """Forget every key."""
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __copy__(self) -> IdentityMap[K, V]:
        other: IdentityMap[K, V] = IdentityMap()
        other._items = dict(self._items)
        return other


def attached(owner: object, name: str, *, create: bool = False) -> IdentityMap[Any, Any]:
    """*owner*'s :class:`IdentityMap` called *name*; an empty one when it has none.

    With *create* the empty map is set on *owner*, so later writes are kept.
    """
    found = getattr(owner, name, None)
    if isinstance(found, IdentityMap):
        return found
    made: IdentityMap[Any, Any] = IdentityMap()
    if create:
        setattr(owner, name, made)
    return made


__all__ = ["IdentityMap", "attached"]
