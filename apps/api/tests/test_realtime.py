import asyncio

import pytest

from app.services.events import EventBus


@pytest.mark.asyncio
async def test_event_bus_delivers_published_event_to_subscriber():
    """SPEC 34: real-time delivery to connected clients. Tested against the
    bus directly (not a live HTTP stream) so the test stays fast and
    deterministic instead of depending on network timing."""
    bus = EventBus()
    queue = bus.subscribe()

    await bus.publish({"id": "evt-1", "type": "motion"})

    delivered = await asyncio.wait_for(queue.get(), timeout=1)
    assert delivered == {"id": "evt-1", "type": "motion"}


@pytest.mark.asyncio
async def test_event_bus_unsubscribe_stops_delivery():
    bus = EventBus()
    queue = bus.subscribe()
    bus.unsubscribe(queue)

    await bus.publish({"id": "evt-2", "type": "motion"})

    assert queue.empty()


@pytest.mark.asyncio
async def test_event_bus_supports_multiple_subscribers():
    bus = EventBus()
    q1, q2 = bus.subscribe(), bus.subscribe()

    await bus.publish({"id": "evt-3", "type": "doorbell"})

    assert (await asyncio.wait_for(q1.get(), timeout=1))["id"] == "evt-3"
    assert (await asyncio.wait_for(q2.get(), timeout=1))["id"] == "evt-3"
