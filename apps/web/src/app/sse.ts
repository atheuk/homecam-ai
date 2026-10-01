export class SseResponseError extends Error {
  constructor(readonly status: number) {
    super(`SSE request failed (HTTP ${status}).`);
  }
}

/** Stream named server-sent events through fetch so the request can carry
 * the same bearer token as the rest of the API. */
export async function consumeSse(
  url: string,
  token: string,
  signal: AbortSignal,
  onEvent: (type: string, data: string) => void,
): Promise<void> {
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "include",
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new SseResponseError(response.status);
  if (!response.body) throw new Error("The event stream returned no body.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener("abort", cancel, { once: true });

  const dispatch = (frame: string) => {
    let type = "message";
    const data: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) type = line.slice(6).trimStart();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length) onEvent(type, data.join("\n"));
  };

  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        dispatch(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
      if (done) break;
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}
