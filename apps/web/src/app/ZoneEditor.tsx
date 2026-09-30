"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type CameraZoneShape = {
  id: string;
  camera_id: string;
  name: string;
  kind: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  points?: number[][] | null;
};

type Still = {
  image: string;
  width: number | null;
  height: number | null;
  source: string;
  captured_at: string;
};

type Props = {
  apiBase: string;
  token: string;
  cameraId: string;
  zoneKinds: readonly string[];
  kindHints: Record<string, string>;
  zones: CameraZoneShape[];
  /** Called after a zone is created or updated so the parent can reload. */
  onSaved: () => void | Promise<void>;
};

/** Minimum points that can enclose an area. */
export const MIN_ZONE_POINTS = 3;
export const MAX_ZONE_POINTS = 64;

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}

/** The polygon a zone should be drawn with: its outline, or its rectangle. */
export function zoneOutline(zone: CameraZoneShape): number[][] {
  if (zone.points && zone.points.length >= MIN_ZONE_POINTS) return zone.points;
  return [
    [zone.x1, zone.y1],
    [zone.x2, zone.y1],
    [zone.x2, zone.y2],
    [zone.x1, zone.y2],
  ];
}

function toPolygonAttr(points: number[][]): string {
  return points.map(([x, y]) => `${x},${y}`).join(" ");
}

export default function ZoneEditor({
  apiBase,
  token,
  cameraId,
  zoneKinds,
  kindHints,
  zones,
  onSaved,
}: Props) {
  const [still, setStill] = useState<Still | null>(null);
  const [stillState, setStillState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [stillError, setStillError] = useState<string | null>(null);
  const [points, setPoints] = useState<number[][]>([]);
  const [name, setName] = useState("mailbox");
  const [kind, setKind] = useState("mailbox");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [manualX, setManualX] = useState("0.5");
  const [manualY, setManualY] = useState("0.5");
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  // Last failure per camera. A camera that just failed is never contacted
  // again on its own — only when the user asks — so flipping between
  // disconnected channels cannot turn into a request storm.
  const failures = useRef<Record<string, string>>({});

  const loadStill = useCallback(
    async (force: boolean) => {
      if (!cameraId || !token) return;
      const previousFailure = failures.current[cameraId];
      if (!force && previousFailure) {
        setStill(null);
        setStillError(previousFailure);
        setStillState("error");
        return;
      }
      setStillState("loading");
      setStillError(null);
      try {
        const r = await fetch(`${apiBase}/api/v1/admin/cameras/${cameraId}/still`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!r.ok) {
          let detail = "Could not get a current picture from this camera.";
          try {
            const body = await r.json();
            if (body?.detail) detail = String(body.detail);
          } catch {
            /* keep the generic message */
          }
          setStill(null);
          failures.current[cameraId] = detail;
          setStillError(detail);
          setStillState("error");
          return;
        }
        delete failures.current[cameraId];
        setStill((await r.json()) as Still);
        setStillState("ready");
      } catch {
        const detail = "Could not reach the API to fetch a picture.";
        setStill(null);
        failures.current[cameraId] = detail;
        setStillError(detail);
        setStillState("error");
      }
    },
    [apiBase, cameraId, token],
  );

  useEffect(() => {
    setStill(null);
    setStillState("idle");
    setStillError(null);
    setPoints([]);
    setEditingId(null);
    setStatus(null);
    setError(null);
    loadStill(false);
  }, [cameraId, loadStill]);

  function addPoint(x: number, y: number) {
    setError(null);
    setPoints((current) => {
      if (current.length >= MAX_ZONE_POINTS) return current;
      return [...current, [round(clamp01(x)), round(clamp01(y))]];
    });
  }

  function handleSurfaceClick(event: React.MouseEvent<HTMLDivElement>) {
    const node = surfaceRef.current;
    if (!node) return;
    const rect = node.getBoundingClientRect();
    if (!rect.width || !rect.height) return; // not laid out yet
    addPoint((event.clientX - rect.left) / rect.width, (event.clientY - rect.top) / rect.height);
  }

  function handleSurfaceTouch(event: React.TouchEvent<HTMLDivElement>) {
    const node = surfaceRef.current;
    const touch = event.changedTouches[0];
    if (!node || !touch) return;
    const rect = node.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    event.preventDefault();
    addPoint((touch.clientX - rect.left) / rect.width, (touch.clientY - rect.top) / rect.height);
  }

  function undo() {
    setPoints((current) => current.slice(0, -1));
  }

  function clear() {
    setPoints([]);
    setStatus(null);
    setError(null);
  }

  function startEditing(zone: CameraZoneShape) {
    setEditingId(zone.id);
    setName(zone.name);
    setKind(zone.kind);
    setPoints(zoneOutline(zone).map(([x, y]) => [x, y]));
    setStatus(`Editing “${zone.name}”. Redraw the shape, then save.`);
    setError(null);
  }

  function stopEditing() {
    setEditingId(null);
    setPoints([]);
    setStatus(null);
    setError(null);
  }

  async function save() {
    if (!token || !cameraId) return;
    if (points.length < MIN_ZONE_POINTS) {
      setError(`Draw at least ${MIN_ZONE_POINTS} points before saving.`);
      return;
    }
    if (!name.trim()) {
      setError("Give the zone a name.");
      return;
    }
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      const url = editingId
        ? `${apiBase}/api/v1/admin/cameras/${cameraId}/zones/${editingId}`
        : `${apiBase}/api/v1/admin/cameras/${cameraId}/zones`;
      const r = await fetch(url, {
        method: editingId ? "PUT" : "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), kind, points }),
      });
      if (!r.ok) {
        setError(
          "Could not save the zone. It needs at least 3 points inside the picture that enclose an area.",
        );
        return;
      }
      setStatus(editingId ? "Zone updated." : "Zone saved.");
      setEditingId(null);
      setPoints([]);
      await onSaved();
    } catch {
      setError("Could not reach the API to save the zone.");
    } finally {
      setBusy(false);
    }
  }

  const drawn = toPolygonAttr(points);
  const cameraZones = zones.filter((zone) => zone.camera_id === cameraId);

  return (
    <div className="zone-editor">
      <div className="zone-editor-canvas">
        {stillState === "loading" && <p className="muted">Fetching a current picture…</p>}
        {stillState === "error" && (
          <div className="zone-editor-error" role="alert">
            <p className="error">{stillError}</p>
            <button type="button" onClick={() => loadStill(true)}>
              Retry picture
            </button>
            <p className="muted">
              You can still draw using the coordinate fields below, or use the manual rectangle form.
            </p>
          </div>
        )}
        <div
          ref={surfaceRef}
          className="zone-editor-surface"
          role="application"
          aria-label="Zone drawing surface. Click or tap to add a point, or use the point coordinate fields."
          onClick={handleSurfaceClick}
          onTouchEnd={handleSurfaceTouch}
        >
          {still && (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={still.image} alt={`Current view from camera ${cameraId}`} />
          )}
          <svg
            className="zone-editor-overlay"
            viewBox="0 0 1 1"
            preserveAspectRatio="none"
            aria-hidden="true"
            focusable="false"
          >
            {cameraZones
              .filter((zone) => zone.id !== editingId)
              .map((zone) => (
                <polygon
                  key={zone.id}
                  className="zone-editor-existing"
                  points={toPolygonAttr(zoneOutline(zone))}
                />
              ))}
            {points.length > 0 && (
              <polygon className="zone-editor-shape" points={drawn} data-testid="zone-shape" />
            )}
            {points.map(([x, y], index) => (
              <circle key={`${x}-${y}-${index}`} cx={x} cy={y} r={0.012} className="zone-editor-point" />
            ))}
          </svg>
        </div>
        {still && (
          <p className="muted">
            {still.source === "stream" ? "Live stream frame" : "Camera snapshot"}
            {still.width && still.height ? ` · ${still.width}×${still.height}` : ""}
          </p>
        )}
      </div>

      <div className="zone-editor-controls">
        <p className="muted" data-testid="zone-point-count">
          {points.length} point{points.length === 1 ? "" : "s"} drawn
          {points.length > 0 && points.length < MIN_ZONE_POINTS
            ? ` — at least ${MIN_ZONE_POINTS} are needed`
            : ""}
        </p>
        <div className="admin-actions">
          <button type="button" onClick={undo} disabled={points.length === 0}>
            Undo point
          </button>
          <button type="button" onClick={clear} disabled={points.length === 0}>
            Clear shape
          </button>
          <button type="button" onClick={() => loadStill(true)} disabled={stillState === "loading"}>
            Refresh picture
          </button>
        </div>

        <fieldset className="channel-editor">
          <legend>Add a point by coordinate (0–1)</legend>
          <div className="channel-row">
            <input
              aria-label="Point x"
              type="number"
              step="0.01"
              min={0}
              max={1}
              value={manualX}
              onChange={(e) => setManualX(e.target.value)}
            />
            <input
              aria-label="Point y"
              type="number"
              step="0.01"
              min={0}
              max={1}
              value={manualY}
              onChange={(e) => setManualY(e.target.value)}
            />
            <button
              type="button"
              onClick={() => {
                const x = Number(manualX);
                const y = Number(manualY);
                if (Number.isFinite(x) && Number.isFinite(y)) addPoint(x, y);
              }}
            >
              Add point
            </button>
          </div>
        </fieldset>

        <label>
          Drawn zone name
          <input value={name} onChange={(e) => setName(e.target.value)} aria-label="Drawn zone name" />
        </label>
        <label>
          Drawn zone kind
          <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Drawn zone kind">
            {zoneKinds.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        {kindHints[kind] && <p className="muted zone-kind-hint">{kindHints[kind]}</p>}

        <div className="admin-actions">
          <button type="button" onClick={save} disabled={busy || !cameraId}>
            {editingId ? "Save changes" : "Save drawn zone"}
          </button>
          {editingId && (
            <button type="button" onClick={stopEditing}>
              Cancel edit
            </button>
          )}
        </div>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {status && <p>{status}</p>}

        {cameraZones.length > 0 && (
          <ul className="provider-list">
            {cameraZones.map((zone) => (
              <li key={zone.id} className="provider-row">
                <span className="provider-type">{zone.kind}</span>
                <span className="provider-summary">
                  {zone.name} — {zone.points ? `${zone.points.length}-point shape` : "rectangle"}
                </span>
                <div className="provider-row-actions">
                  <button type="button" onClick={() => startEditing(zone)} aria-label={`Edit zone ${zone.name}`}>
                    Edit shape
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
