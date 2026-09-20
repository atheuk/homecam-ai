import "@testing-library/jest-dom/vitest";
class MockEventSource { addEventListener(){} close(){} }
// jsdom does not provide EventSource; this keeps dashboard tests deterministic.
Object.assign(globalThis, { EventSource: MockEventSource });
