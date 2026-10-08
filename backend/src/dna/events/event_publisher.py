"""In-memory event publisher for broadcasting events."""

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine, Optional

from fastapi import WebSocket

from dna.events.bus import EventBus
from dna.events.event_types import EventType

logger = logging.getLogger(__name__)

EventCallback = Callable[[EventType, dict[str, Any]], Coroutine[Any, Any, None]]

_publisher: "EventPublisher | None" = None


class WebSocketManager:
    """Manages WebSocket connections for broadcasting events."""

    def __init__(self):
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Register a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info(
            "WebSocket client connected. Total connections: %d", len(self._connections)
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        async with self._lock:
            self._connections.discard(websocket)
        logger.info(
            "WebSocket client disconnected. Total connections: %d",
            len(self._connections),
        )

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Broadcast a message to all connected WebSocket clients."""
        if not self._connections:
            return

        message_json = json.dumps(message)
        disconnected: list[WebSocket] = []

        async with self._lock:
            connections = list(self._connections)

        for websocket in connections:
            try:
                await websocket.send_text(message_json)
            except Exception as e:
                logger.warning("Failed to send to WebSocket client: %s", e)
                disconnected.append(websocket)

        if disconnected:
            async with self._lock:
                for ws in disconnected:
                    self._connections.discard(ws)
            logger.info(
                "Removed %d disconnected clients. Total connections: %d",
                len(disconnected),
                len(self._connections),
            )

    @property
    def connection_count(self) -> int:
        """Get the number of active connections."""
        return len(self._connections)


class EventPublisher:
    """In-memory event publisher that broadcasts to registered subscribers and WebSocket clients."""

    def __init__(self):
        self._subscribers: dict[EventType, list[EventCallback]] = {}
        self._global_subscribers: list[EventCallback] = []
        self._ws_manager = WebSocketManager()
        # Other backend instances' browsers.  None: this is the only instance.
        self._bus: Optional[EventBus] = None

    @property
    def ws_manager(self) -> WebSocketManager:
        """Get the WebSocket manager."""
        return self._ws_manager

    async def connect(self) -> None:
        """No-op for compatibility. In-memory publisher doesn't need connection."""
        pass

    def subscribe(
        self, event_type: EventType, callback: EventCallback
    ) -> Callable[[], None]:
        """Subscribe to a specific event type.

        Returns an unsubscribe function.
        """
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
        logger.debug("Subscribed to event type: %s", event_type.value)

        def unsubscribe() -> None:
            if (
                event_type in self._subscribers
                and callback in self._subscribers[event_type]
            ):
                self._subscribers[event_type].remove(callback)
                logger.debug("Unsubscribed from event type: %s", event_type.value)

        return unsubscribe

    def subscribe_all(self, callback: EventCallback) -> Callable[[], None]:
        """Subscribe to all event types.

        Returns an unsubscribe function.
        """
        self._global_subscribers.append(callback)
        logger.debug("Subscribed to all events")

        def unsubscribe() -> None:
            if callback in self._global_subscribers:
                self._global_subscribers.remove(callback)
                logger.debug("Unsubscribed from all events")

        return unsubscribe

    async def publish(self, event_type: EventType, payload: dict[str, Any]) -> None:
        """Publish an event to all subscribers and WebSocket clients."""
        logger.info("Publishing event: %s", event_type.value)

        callbacks_to_call: list[EventCallback] = []

        if event_type in self._subscribers:
            callbacks_to_call.extend(self._subscribers[event_type])

        callbacks_to_call.extend(self._global_subscribers)

        for callback in callbacks_to_call:
            try:
                await callback(event_type, payload)
            except Exception as e:
                logger.exception("Error in event subscriber callback: %s", e)

        await self.broadcast({"type": event_type.value, "payload": payload})

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send a message to every connected browser, on every backend instance.

        This instance's own browsers get it directly; other instances' get it through
        the bus.  Everything meant for browsers goes through here so that none is
        confined to the instance that happened to raise it.
        """
        await self._ws_manager.broadcast(message)
        if self._bus is not None:
            await self._bus.publish(message)

    async def deliver_remote(self, message: dict[str, Any]) -> None:
        """Hand a message another instance raised to this instance's browsers.  It is
        not passed to local subscribers (that instance already ran them) and not sent
        back to the bus (that would loop)."""
        await self._ws_manager.broadcast(message)

    async def attach_bus(self, bus: EventBus) -> None:
        """Start sharing events with the other instances through ``bus``."""
        await bus.start(self.deliver_remote)  # raises if the bus cannot start
        self._bus = bus  # attached only once it is running

    @property
    def bus(self) -> Optional[EventBus]:
        return self._bus

    async def close(self) -> None:
        """Clear all subscribers and leave the bus."""
        if self._bus is not None:
            await self._bus.stop()
            self._bus = None
        self._subscribers.clear()
        self._global_subscribers.clear()
        logger.info("Event publisher closed, all subscribers cleared")


def get_event_publisher() -> EventPublisher:
    """Get the singleton EventPublisher instance."""
    global _publisher
    if _publisher is None:
        _publisher = EventPublisher()
    return _publisher


def reset_event_publisher() -> None:
    """Reset the singleton for testing purposes."""
    global _publisher
    _publisher = None
