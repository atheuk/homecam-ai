import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import ZoneEditor, { zoneOutline } from "./ZoneEditor";

const STILL = {
  image: "data:image/jpeg;base64,AAAA",
  width: 1920,
  height: 1080,
  source: "stream",
  captured_at: new Date().toISOString(),
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

function mockFetch(handlers: Record<string, (init?: RequestInit) => Response>) {
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url);
    for (const [key, handler] of Object.entries(handlers)) {
      if (path.includes(key)) return handler(init);
    }
    return jsonResponse({ detail: "not found" }, 404);
  });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

/** jsdom gives every element a zero-sized box; give the surface a real one. */
function sizeSurface(width = 200, height = 100) {
  const surface = screen.getByRole("application");
  surface.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width, height, right: width, bottom: height, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
  return surface;
}

function renderEditor(props: Partial<React.ComponentProps<typeof ZoneEditor>> = {}) {
  const onSaved = vi.fn();
  render(
    <ZoneEditor
      apiBase="http://api.test"
      token="tok"
      cameraId="cam-1"
      zoneKinds={["driveway", "mailbox", "bin", "other"]}
      kindHints={{ mailbox: "Mailbox guidance." }}
      zones={[]}
      onSaved={onSaved}
      {...props}
    />,
  );
  return { onSaved };
}

function clickAt(surface: HTMLElement, x: number, y: number) {
  fireEvent.click(surface, { clientX: x, clientY: y });
}

describe("ZoneEditor", () => {
  beforeEach(() => {
    mockFetch({ "/still": () => jsonResponse(STILL) });
  });

  it("fetches a current still for the selected camera and shows where it came from", async () => {
    renderEditor();
    expect(await screen.findByAltText("Current view from camera cam-1")).toBeInTheDocument();
    expect(screen.getByText(/Live stream frame · 1920×1080/)).toBeInTheDocument();
  });

  it("keeps the faster selected camera still when an earlier camera request finishes later", async () => {
    let resolveA!: (response: Response) => void;
    const delayedA = new Promise<Response>((resolve) => {
      resolveA = resolve;
    });
    global.fetch = vi.fn((url: RequestInfo | URL) =>
      String(url).includes("cam-A")
        ? delayedA
        : Promise.resolve(jsonResponse({ ...STILL, image: "data:image/jpeg;base64,camera-B" })),
    ) as unknown as typeof fetch;

    const editor = (cameraId: string) => (
      <ZoneEditor
        apiBase="http://api.test"
        token="tok"
        cameraId={cameraId}
        zoneKinds={["mailbox"]}
        kindHints={{}}
        zones={[]}
        onSaved={vi.fn()}
      />
    );
    const { rerender } = render(editor("cam-A"));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    rerender(editor("cam-B"));
    const imageB = await screen.findByAltText("Current view from camera cam-B");
    expect(imageB).toHaveAttribute("src", "data:image/jpeg;base64,camera-B");

    await act(async () => {
      resolveA(jsonResponse({ ...STILL, image: "data:image/jpeg;base64,camera-A" }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(screen.getByAltText("Current view from camera cam-B")).toHaveAttribute(
      "src",
      "data:image/jpeg;base64,camera-B",
    );
    expect(screen.queryByText("Could not reach the API to fetch a picture.")).not.toBeInTheDocument();
  });

  it("does not show an error when an earlier camera request rejects after switching", async () => {
    let rejectA!: (error: Error) => void;
    const delayedA = new Promise<Response>((_resolve, reject) => {
      rejectA = reject;
    });
    global.fetch = vi.fn((url: RequestInfo | URL) =>
      String(url).includes("cam-A")
        ? delayedA
        : Promise.resolve(jsonResponse({ ...STILL, image: "data:image/jpeg;base64,camera-B" })),
    ) as unknown as typeof fetch;

    const editor = (cameraId: string) => (
      <ZoneEditor
        apiBase="http://api.test"
        token="tok"
        cameraId={cameraId}
        zoneKinds={["mailbox"]}
        kindHints={{}}
        zones={[]}
        onSaved={vi.fn()}
      />
    );
    const { rerender } = render(editor("cam-A"));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    rerender(editor("cam-B"));
    await screen.findByAltText("Current view from camera cam-B");

    await act(async () => {
      rejectA(new Error("network request aborted"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(screen.queryByText("Could not reach the API to fetch a picture.")).not.toBeInTheDocument();
    expect(screen.getByAltText("Current view from camera cam-B")).toHaveAttribute(
      "src",
      "data:image/jpeg;base64,camera-B",
    );
  });

  it("asks for the still with the admin token, not an unauthenticated image URL", async () => {
    const fetchMock = mockFetch({ "/still": () => jsonResponse(STILL) });
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit & { headers: Record<string, string> }).headers.Authorization).toBe("Bearer tok");
  });

  it("turns clicks on the picture into normalized points", async () => {
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    const surface = sizeSurface(200, 100);
    clickAt(surface, 100, 50);
    clickAt(surface, 200, 100);
    expect(screen.getByTestId("zone-point-count")).toHaveTextContent("2 points drawn");
    expect(screen.getByTestId("zone-shape").getAttribute("points")).toBe("0.5,0.5 1,1");
  });

  it("keeps points inside the picture even when the click escapes it", async () => {
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    const surface = sizeSurface(200, 100);
    clickAt(surface, -50, 500);
    expect(screen.getByTestId("zone-shape").getAttribute("points")).toBe("0,1");
  });

  it("supports touch drawing", async () => {
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    const surface = sizeSurface(200, 100);
    fireEvent.touchEnd(surface, { changedTouches: [{ clientX: 50, clientY: 25 }] });
    expect(screen.getByTestId("zone-shape").getAttribute("points")).toBe("0.25,0.25");
  });

  it("undoes the last point and clears the whole shape", async () => {
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    const surface = sizeSurface(200, 100);
    clickAt(surface, 20, 20);
    clickAt(surface, 40, 40);
    fireEvent.click(screen.getByText("Undo point"));
    expect(screen.getByTestId("zone-point-count")).toHaveTextContent("1 point drawn");
    fireEvent.click(screen.getByText("Clear shape"));
    expect(screen.getByTestId("zone-point-count")).toHaveTextContent("0 points drawn");
    expect(screen.queryByTestId("zone-shape")).not.toBeInTheDocument();
  });

  it("refuses to save a shape that cannot enclose an area", async () => {
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    const surface = sizeSurface(200, 100);
    clickAt(surface, 20, 20);
    clickAt(surface, 40, 40);
    fireEvent.click(screen.getByText("Save drawn zone"));
    expect(await screen.findByText("Draw at least 3 points before saving.")).toBeInTheDocument();
  });

  it("posts a polygon zone with its name and kind for the selected camera", async () => {
    let body: unknown = null;
    const fetchMock = mockFetch({
      "/still": () => jsonResponse(STILL),
      "/zones": (init) => {
        body = JSON.parse(String(init?.body));
        return jsonResponse({ id: "z1" }, 201);
      },
    });
    const { onSaved } = renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    const surface = sizeSurface(200, 100);
    clickAt(surface, 20, 10);
    clickAt(surface, 100, 10);
    clickAt(surface, 60, 80);
    fireEvent.change(screen.getByLabelText("Drawn zone name"), { target: { value: "Front mailbox" } });
    fireEvent.change(screen.getByLabelText("Drawn zone kind"), { target: { value: "mailbox" } });
    fireEvent.click(screen.getByText("Save drawn zone"));

    await screen.findByText("Zone saved.");
    expect(body).toEqual({
      name: "Front mailbox",
      kind: "mailbox",
      points: [
        [0.1, 0.1],
        [0.5, 0.1],
        [0.3, 0.8],
      ],
    });
    const zoneCall = fetchMock.mock.calls.find(([url]) => String(url).includes("/zones"));
    expect(String(zoneCall?.[0])).toBe("http://api.test/api/v1/admin/cameras/cam-1/zones");
    expect(onSaved).toHaveBeenCalled();
  });

  it("includes a loitering dwell threshold only when one is entered", async () => {
    let body: Record<string, unknown> = {};
    mockFetch({
      "/still": () => jsonResponse(STILL),
      "/zones": (init) => {
        body = JSON.parse(String(init?.body));
        return jsonResponse({ id: "z1" }, 201);
      },
    });
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    const surface = sizeSurface(200, 100);
    clickAt(surface, 20, 10);
    clickAt(surface, 100, 10);
    clickAt(surface, 60, 80);
    fireEvent.change(screen.getByLabelText("Loitering seconds"), { target: { value: "90" } });
    fireEvent.click(screen.getByText("Save drawn zone"));

    await screen.findByText("Zone saved.");
    expect(body.dwell_seconds).toBe(90);
  });

  it("rejects a non-positive loitering threshold before contacting the API", async () => {
    const fetchMock = mockFetch({ "/still": () => jsonResponse(STILL) });
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    const surface = sizeSurface(200, 100);
    clickAt(surface, 20, 10);
    clickAt(surface, 100, 10);
    clickAt(surface, 60, 80);
    fireEvent.change(screen.getByLabelText("Loitering seconds"), { target: { value: "0" } });
    fireEvent.click(screen.getByText("Save drawn zone"));

    expect(await screen.findByText("Loitering seconds must be a positive number.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/zones"))).toBe(false);
  });

  it("shows the guidance for the selected kind", async () => {
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    expect(screen.getByText("Mailbox guidance.")).toBeInTheDocument();
  });

  it("reports a rejected zone inline instead of silently failing", async () => {
    mockFetch({
      "/still": () => jsonResponse(STILL),
      "/zones": () => jsonResponse({ detail: "bad" }, 422),
    });
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    const surface = sizeSurface(200, 100);
    clickAt(surface, 20, 10);
    clickAt(surface, 100, 10);
    clickAt(surface, 60, 80);
    fireEvent.click(screen.getByText("Save drawn zone"));
    expect(await screen.findByText(/Could not save the zone/)).toBeInTheDocument();
  });

  it("explains a camera that cannot produce a picture and offers a retry", async () => {
    const fetchMock = mockFetch({
      "/still": () => jsonResponse({ detail: "Camera 'cam-1' is offline, so it has no current picture to draw on." }, 503),
    });
    renderEditor();
    expect(await screen.findByText(/is offline/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText("Retry picture"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("does not re-fetch a failed camera on its own", async () => {
    const fetchMock = mockFetch({ "/still": () => jsonResponse({ detail: "nope" }, 503) });
    renderEditor();
    await screen.findByText("nope");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("still explains the failure when the user comes back to that camera", async () => {
    const fetchMock = mockFetch({ "/still": () => jsonResponse({ detail: "nope" }, 503) });
    const { rerender } = render(
      <ZoneEditor
        apiBase="http://api.test"
        token="tok"
        cameraId="cam-1"
        zoneKinds={["mailbox"]}
        kindHints={{}}
        zones={[]}
        onSaved={vi.fn()}
      />,
    );
    await screen.findByText("nope");
    const editorFor = (cameraId: string) => (
      <ZoneEditor
        apiBase="http://api.test"
        token="tok"
        cameraId={cameraId}
        zoneKinds={["mailbox"]}
        kindHints={{}}
        zones={[]}
        onSaved={vi.fn()}
      />
    );
    rerender(editorFor("cam-2"));
    await screen.findByText("nope");
    rerender(editorFor("cam-1"));
    // Back on the failed camera: the reason is shown again from cache, and
    // the disconnected channel is never contacted a second time.
    expect(await screen.findByText("nope")).toBeInTheDocument();
    expect(screen.getByText("Retry picture")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("loads an existing zone's shape for editing and saves it with PUT", async () => {
    let method: string | undefined;
    mockFetch({
      "/still": () => jsonResponse(STILL),
      "/zones/z1": (init) => {
        method = init?.method;
        return jsonResponse({ id: "z1" });
      },
    });
    renderEditor({
      zones: [
        {
          id: "z1",
          camera_id: "cam-1",
          name: "Curb",
          kind: "bin",
          x1: 0.1,
          y1: 0.2,
          x2: 0.4,
          y2: 0.6,
          points: [
            [0.1, 0.2],
            [0.4, 0.2],
            [0.3, 0.6],
          ],
        },
      ],
    });
    await screen.findByAltText("Current view from camera cam-1");
    expect(screen.getByText("Curb — 3-point shape")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Edit zone Curb"));
    expect(screen.getByTestId("zone-point-count")).toHaveTextContent("3 points drawn");
    expect((screen.getByLabelText("Drawn zone name") as HTMLInputElement).value).toBe("Curb");
    fireEvent.click(screen.getByText("Save changes"));
    await screen.findByText("Zone updated.");
    expect(method).toBe("PUT");
  });

  it("lets a point be added by coordinate for keyboard users", async () => {
    renderEditor();
    await screen.findByAltText("Current view from camera cam-1");
    fireEvent.change(screen.getByLabelText("Point x"), { target: { value: "0.25" } });
    fireEvent.change(screen.getByLabelText("Point y"), { target: { value: "0.75" } });
    fireEvent.click(screen.getByText("Add point"));
    expect(screen.getByTestId("zone-shape").getAttribute("points")).toBe("0.25,0.75");
  });
});

describe("zoneOutline", () => {
  it("uses the drawn polygon when there is one", () => {
    expect(
      zoneOutline({
        id: "z",
        camera_id: "c",
        name: "n",
        kind: "mailbox",
        x1: 0,
        y1: 0,
        x2: 1,
        y2: 1,
        points: [
          [0, 0],
          [1, 0],
          [0.5, 1],
        ],
      }),
    ).toEqual([
      [0, 0],
      [1, 0],
      [0.5, 1],
    ]);
  });

  it("falls back to the rectangle corners for legacy zones", () => {
    expect(
      zoneOutline({ id: "z", camera_id: "c", name: "n", kind: "driveway", x1: 0.1, y1: 0.2, x2: 0.3, y2: 0.4 }),
    ).toEqual([
      [0.1, 0.2],
      [0.3, 0.2],
      [0.3, 0.4],
      [0.1, 0.4],
    ]);
  });
});
