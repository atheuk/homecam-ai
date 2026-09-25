import "@testing-library/jest-dom/vitest";
class MockEventSource { addEventListener(){} close(){} }
// jsdom does not provide EventSource; this keeps dashboard tests deterministic.
Object.assign(globalThis, { EventSource: MockEventSource });

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
