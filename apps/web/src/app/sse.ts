export class SseResponseError extends Error {
  constructor(readonly status: number) {
    super(`SSE request failed (HTTP ${status}).`);
  }
}

class SseTransportError extends Error {
  readonly cause?: unknown;

  constructor(message: string, cause?: unknown) {
    super(message);
    this.cause = cause;
  }
}

const INITIAL_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 30_000;

function waitForReconnect(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, delayMs);
    signal.addEventListener("abort", finish, { once: true });
  });
}

/** Stream named server-sent events through fetch so the request can carry
 * the same bearer token as the rest of the API. Transient disconnects retry
 * with capped exponential backoff; authentication and other client errors
 * remain terminal so callers can clear invalid sessions. */
export async function consumeSse(
  url: string,
  token: string | null,
  signal: AbortSignal,
  onEvent: (type: string, data: string) => void,
): Promise<void> {
  const dispatch = (frame: string) => {
    let type = "message";
    const data: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) type = line.slice(6).trimStart();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length) onEvent(type, data.join("\n"));
  };

  let reconnectAttempt = 0;
  while (!signal.aborted) {
    try {
      let response: Response;
      try {
        response = await fetch(url, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          credentials: "include",
          cache: "no-store",
          signal,
        });
      } catch (error) {
        if (signal.aborted) return;
        throw new SseTransportError("Could not connect to the event stream.", error);
      }
      if (!response.ok) {
        if (response.status < 500) throw new SseResponseError(response.status);
      } else {
        if (!response.body) throw new Error("The event stream returned no body.");

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let closed = false;
        const cancel = () => { void reader.cancel().catch(() => undefined); };
        signal.addEventListener("abort", cancel, { once: true });
        try {
          while (!signal.aborted) {
            let result: ReadableStreamReadResult<Uint8Array>;
            try {
              result = await reader.read();
            } catch (error) {
              if (signal.aborted) break;
              throw new SseTransportError("The event stream disconnected.", error);
            }
            const { done, value } = result;
            buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
            let boundary = buffer.indexOf("\n\n");
            while (boundary >= 0) {
              dispatch(buffer.slice(0, boundary));
              buffer = buffer.slice(boundary + 2);
              boundary = buffer.indexOf("\n\n");
            }
            if (done) {
              closed = true;
              break;
            }
          }
        } finally {
          signal.removeEventListener("abort", cancel);
          if (!closed) cancel();
          reader.releaseLock();
        }
      }
    } catch (error) {
      if (signal.aborted) return;
      if (!(error instanceof SseTransportError) &&
          !(error instanceof SseResponseError && error.status >= 500)) {
        throw error;
      }
    }

    if (signal.aborted) return;
    const delay = Math.min(
      INITIAL_RECONNECT_DELAY_MS * 2 ** reconnectAttempt,
      MAX_RECONNECT_DELAY_MS,
    );
    reconnectAttempt = Math.min(reconnectAttempt + 1, 5);
    await waitForReconnect(delay, signal);
  }
}
