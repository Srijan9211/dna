"""Entity ids.

ShotGrid ids are integers.  Other trackers use their own: AYON ids are 32-character
UUIDs, and every AYON lookup also needs the project name, so DNA carries it inside
the id (``"<project>:<uuid>"``).  Rather than force one shape on every tracker, DNA
treats an id as *opaque*: an ``int`` or a ``str`` that it stores, compares for
equality and hands back to the connector that issued it, and never interprets.

``normalize_id`` is the single rule for turning an incoming value (a JSON number, a
JSON string, a URL path segment) into an id.  A short run of digits becomes an
``int`` so ShotGrid keeps working when an id arrives as text; anything else stays a
string.  The 15-digit ceiling keeps every real string id out of reach: an AYON UUID
is 32 characters, so it can never be mistaken for a number.
"""

from __future__ import annotations

import re
from typing import Any, Union

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic_core import core_schema

_DIGITS = re.compile(r"\d{1,15}")


def normalize_id(value: Any) -> Union[int, str]:
    """Return ``value`` as an opaque entity id (``int`` or non-numeric ``str``).

    Raises:
        ValueError: ``value`` cannot be an id (a bool, an empty string, a float
            with a fraction, or another type).
    """
    if isinstance(value, bool):
        raise ValueError("an entity id cannot be a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"an entity id cannot be the fractional number {value}")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("an entity id cannot be empty")
        return int(text) if _DIGITS.fullmatch(text) else text
    raise ValueError(f"an entity id must be an integer or a string, not {type(value).__name__}")


class EntityId:
    """Annotation for a field, body, query or path parameter holding an entity id.

    It is a class carrying its own pydantic schema, not ``Annotated[..., validator]``:
    FastAPI (0.104) silently drops ``Annotated`` validators on *path* parameters,
    which would leave ``/playlists/123`` as the string ``"123"`` and break every
    integer-id tracker.  A schema-bearing class is honoured everywhere.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            normalize_id,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: value
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> dict[str, Any]:
        return {"anyOf": [{"type": "integer"}, {"type": "string"}]}
