import "@testing-library/jest-dom/vitest";

// jsdom does not provide EventSource; this keeps dashboard/security-panel
// tests deterministic while still letting tests prove live SSE updates
// actually happen (Dashboard.tsx's `event.created` listener, and
// SecurityPanel.tsx's `incident.created`/`updated`/`escalated` listeners).
// `instances` lets a test grab whichever EventSource the component under
// test opened and call `.dispatch(type, data)` to simulate a server push
// without a real network connection.
type Listener = (event: MessageEvent) => void;

export class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  closed = false;
  private listeners = new Map<string, Listener[]>();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener) {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  removeEventListener(type: string, listener: Listener) {
    const list = this.listeners.get(type);
    if (!list) return;
    this.listeners.set(type, list.filter((existing) => existing !== listener));
  }

  close() {
    this.closed = true;
  }

  /** Simulate the server pushing one named SSE event to every listener
   * this EventSource instance registered for that event type. */
  dispatch(type: string, data: unknown) {
    const event = { data: JSON.stringify(data) } as MessageEvent;
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}
Object.assign(globalThis, { EventSource: MockEventSource });

export class MockSseStream {
  static instances: MockSseStream[] = [];
  private controller: ReadableStreamDefaultController<Uint8Array> | null = null;

  static response() {
    const stream = new MockSseStream();
    MockSseStream.instances.push(stream);
    return new Response(new ReadableStream<Uint8Array>({
      start: controller => { stream.controller = controller; },
    }), { status: 200, headers: { "Content-Type": "text/event-stream" } });
  }

  dispatch(type: string, data: unknown) {
    this.controller?.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`));
  }
}

// jsdom does not implement PointerEvent either. Without it, fireEvent's
// pointer helpers fall back to a bare Event and silently drop clientX /
// pointerId, so any drag or pinch assertion would be meaningless. A
// MouseEvent-backed shim carries the coordinates the handlers actually read.
if (!("PointerEvent" in globalThis)) {
  class PointerEventShim extends MouseEvent {
    pointerId: number;
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 0;
    }
  }
  Object.assign(globalThis, { PointerEvent: PointerEventShim });
}
// Pointer capture is part of the drag path but is not implemented in jsdom.
if (!Element.prototype.setPointerCapture) {
  Element.prototype.setPointerCapture = function setPointerCapture() {};
  Element.prototype.releasePointerCapture = function releasePointerCapture() {};
}
