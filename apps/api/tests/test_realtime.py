import asyncio

import pytest

from app.api.routes import sse_frame
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


def test_sse_frame_does_not_mutate_shared_payload_across_subscribers():
    """Regression test: EventBus.publish() hands the *same* dict instance to
    every subscriber. sse_frame() must never mutate it, or the first
    subscriber to read an incident/security-mode broadcast would strip
    ``_sse_event`` before a second concurrently-connected tab/subscriber
    sees it, silently mislabeling the message as a plain ``event.created``.
    """
    shared_payload = {"id": "incident-1", "kind": "intrusion", "_sse_event": "incident.created"}

    first_name, first_data = sse_frame(shared_payload)
    second_name, second_data = sse_frame(shared_payload)

    assert first_name == "incident.created"
    assert second_name == "incident.created"
    assert "_sse_event" not in first_data
    assert "_sse_event" not in second_data
    # The original object handed to every subscriber must stay intact.
    assert shared_payload == {"id": "incident-1", "kind": "intrusion", "_sse_event": "incident.created"}


def test_sse_frame_defaults_untagged_payload_to_event_created():
    name, data = sse_frame({"id": "evt-1", "type": "motion"})
    assert name == "event.created"
    assert data == {"id": "evt-1", "type": "motion"}
