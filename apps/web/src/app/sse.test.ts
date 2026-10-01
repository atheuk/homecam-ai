import { afterEach, describe, expect, it, vi } from "vitest";
import { consumeSse, SseResponseError } from "./sse";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("authenticated SSE", () => {
  it("reconnects after the stream ends and stops on abort", async () => {
    const controller = new AbortController();
    const encoder = new TextEncoder();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(new ReadableStream({
        start(stream) {
          stream.enqueue(encoder.encode("event: ready\ndata: {}\n\n"));
          stream.close();
        },
      })))
      .mockResolvedValueOnce(new Response(new ReadableStream({
        start(stream) {
          stream.enqueue(encoder.encode("event: update\ndata: next\n\n"));
        },
      })));
    vi.stubGlobal("fetch", fetchMock);
    const events: string[] = [];

    const consuming = consumeSse("/api/v1/ws", "session-token", controller.signal, (type, data) => {
      events.push(`${type}:${data}`);
      if (type === "update") controller.abort();
    });

    await expect(consuming).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(events).toEqual(["ready:{}", "update:next"]);
  }, 5_000);

  it("does not retry authentication failures", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(consumeSse("/api/v1/ws", "expired-token", new AbortController().signal, vi.fn()))
      .rejects.toBeInstanceOf(SseResponseError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
