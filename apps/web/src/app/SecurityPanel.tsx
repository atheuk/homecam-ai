"use client";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { DigestCard, SearchCard } from "./Insights";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type Mode = "disarmed" | "home" | "away" | "night";
const MODES: { value: Mode; label: string; hint: string }[] = [
  { value: "disarmed", label: "Disarmed", hint: "No zones are armed. Motion is still recorded, nothing raises an incident." },
  { value: "home", label: "Home", hint: "Perimeter zones (driveway, entry, street) are armed. Indoor/unzoned activity does not raise an incident." },
  { value: "away", label: "Away", hint: "Every zone is armed, including unzoned camera views." },
  { value: "night", label: "Night", hint: "Same coverage as Away. Use while everyone is home and asleep." },
];

type SecurityMode = { mode: string; changed_by: string | null; changed_at: string };

type Incident = {
  id: string;
  kind: string;
  status: string;
  severity: string;
  camera_id: string;
  zone: string | null;
  mode_at_creation: string;
  event_count: number;
  first_seen_at: string;
  last_seen_at: string;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  escalation_level: number;
  summary: string;
  ai_summary: string | null;
};

type IncidentEvent = {
  id: string;
  type: string;
  description: string;
  start_time: string;
  photo_url?: string | null;
};

type AuditEntry = {
  id: string;
  actor_user_id: string | null;
  actor_label: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
};

const KIND_LABELS: Record<string, string> = {
  intrusion: "Intrusion",
  camera_offline: "Camera offline",
  camera_obstruction: "Camera obstructed",
  camera_frozen: "Camera feed frozen",
};

const STATUS_LABELS: Record<string, string> = { open: "Open", acknowledged: "Acknowledged", resolved: "Resolved" };

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

function timeAgo(value: string): string {
  const diff = Date.now() - new Date(value).getTime();
  const minutes = Math.round(diff / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/** One incident's evidence: acknowledge/resolve, and the deterministic
 * timeline of grouped events. ``ai_summary`` (if present) is always labelled
 * as AI-generated context, never as a substitute for the deterministic
 * kind/severity/status fields above it. */
function IncidentCard({
  incident, token, cameraName, onChanged,
}: { incident: Incident; token: string; cameraName: string; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [events, setEvents] = useState<IncidentEvent[] | null>(null);

  async function act(action: "acknowledge" | "resolve") {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(`${API}/api/v1/security/incidents/${incident.id}/${action}`, {
        method: "POST",
        headers: authHeaders(token),
      });
      if (!r.ok) {
        setError(`Could not ${action} this incident.`);
        return;
      }
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  async function toggleExpand() {
    if (expanded) {
      setExpanded(false);
      return;
    }
    setExpanded(true);
    if (events) return;
    const r = await fetch(`${API}/api/v1/security/incidents/${incident.id}/export`, { headers: authHeaders(token) });
    if (r.ok) {
      const body = await r.json();
      setEvents(body.events as IncidentEvent[]);
    }
  }

  async function exportEvidence() {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(`${API}/api/v1/security/incidents/${incident.id}/export`, { headers: authHeaders(token) });
      if (!r.ok) {
        setError("Could not export this incident.");
        return;
      }
      const body = await r.json();
      const blob = new Blob([JSON.stringify(body, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `incident-${incident.id}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className={`incident-card severity-${incident.severity}`}>
      <div className="incident-head">
        <div>
          <span className={`badge status-${incident.status}`}>{STATUS_LABELS[incident.status] || incident.status}</span>
          <span className={`badge severity-${incident.severity}`}>{incident.severity} severity</span>
          {incident.escalation_level > 0 && <span className="badge escalation">escalated ×{incident.escalation_level}</span>}
        </div>
        <small>{timeAgo(incident.last_seen_at)}</small>
      </div>
      <h4>{KIND_LABELS[incident.kind] || incident.kind}</h4>
      <p className="incident-summary">{incident.summary}</p>
      {incident.ai_summary && (
        <p className="ai-summary"><span className="badge ai">AI summary</span>{incident.ai_summary}</p>
      )}
      <p className="muted incident-meta">
        {cameraName}{incident.zone ? ` · ${incident.zone}` : ""} · {incident.event_count} event{incident.event_count === 1 ? "" : "s"} ·
        mode was {incident.mode_at_creation}
      </p>
      <div className="incident-actions">
        {incident.status === "open" && <button type="button" disabled={busy} onClick={() => act("acknowledge")}>Acknowledge</button>}
        {incident.status !== "resolved" && <button type="button" disabled={busy} onClick={() => act("resolve")}>Resolve</button>}
        <button type="button" disabled={busy} onClick={exportEvidence}>Export evidence</button>
        <button type="button" className="text-button" onClick={toggleExpand}>{expanded ? "Hide timeline" : "Show timeline"}</button>
      </div>
      {error && <p className="error">{error}</p>}
      {expanded && (
        <ul className="incident-timeline">
          {events === null && <li className="muted">Loading timeline…</li>}
          {events?.map((event) => (
            <li key={event.id}>
              <span className="incident-timeline-time">{new Date(event.start_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
              <span>{event.description || event.type}</span>
            </li>
          ))}
          {events?.length === 0 && <li className="muted">No grouped events.</li>}
        </ul>
      )}
    </article>
  );
}

/** Security & incidents: arming modes, incident acknowledge/resolve/export,
 * and the audit trail. Requires its own sign-in, same single-user-is-admin
 * scope as AdminPanel - see that component's docstring. Every write here is
 * a human action; nothing on this panel is triggered autonomously by AI. */
export default function SecurityPanel({ cameras }: { cameras: { id: string; name: string }[] }) {
  const [token, setToken] = useState<string | null>(null);
  const [loginEmail, setLoginEmail] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);

  const [mode, setModeState] = useState<SecurityMode | null>(null);
  const [modeBusy, setModeBusy] = useState(false);
  const [modeError, setModeError] = useState<string | null>(null);

  const [incidentFilter, setIncidentFilter] = useState<"open" | "all">("open");
  const [incidents, setIncidents] = useState<Incident[] | null>(null);
  const [incidentsError, setIncidentsError] = useState<string | null>(null);

  const [auditLog, setAuditLog] = useState<AuditEntry[] | null>(null);

  const cameraName = useCallback(
    (id: string) => cameras.find((camera) => camera.id === id)?.name || id,
    [cameras],
  );

  const loadMode = useCallback(async (activeToken: string) => {
    const r = await fetch(`${API}/api/v1/security/mode`, { headers: authHeaders(activeToken) });
    if (r.status === 401) {
      setToken(null);
      return;
    }
    if (r.ok) setModeState(await r.json());
  }, []);

  const loadIncidents = useCallback(async (activeToken: string, filter: "open" | "all") => {
    const query = filter === "open" ? "?status=open" : "";
    const r = await fetch(`${API}/api/v1/security/incidents${query}`, { headers: authHeaders(activeToken) });
    if (!r.ok) {
      setIncidentsError("Could not load incidents.");
      return;
    }
    setIncidentsError(null);
    setIncidents(await r.json());
  }, []);

  const loadAuditLog = useCallback(async (activeToken: string) => {
    const r = await fetch(`${API}/api/v1/security/audit-log?limit=50`, { headers: authHeaders(activeToken) });
    if (r.ok) setAuditLog(await r.json());
  }, []);

  useEffect(() => {
    if (!token) return;
    loadMode(token);
    loadIncidents(token, incidentFilter);
    loadAuditLog(token);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => {
    if (token) loadIncidents(token, incidentFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incidentFilter]);

  // `incidentFilter` is read from a ref (not a hook dep) so an open/all
  // toggle doesn't tear down and reopen the SSE connection.
  const incidentFilterRef = useRef(incidentFilter);
  useEffect(() => {
    incidentFilterRef.current = incidentFilter;
  }, [incidentFilter]);

  // Live incident updates: without this, `openCount` and the incident list
  // only reflected reality on page load/reload (see review finding #2 -
  // `incident.created`/`updated`/`escalated` never refreshed the panel).
  // This reuses the same `/api/v1/ws` bus Dashboard.tsx already subscribes
  // to for `event.created`; incident mutations are deterministic writes
  // (see IncidentCard's docstring), so re-fetching on any of these three
  // events is always safe, never AI-triggered.
  useEffect(() => {
    if (!token) return;
    const source = new EventSource(`${API}/api/v1/ws`);
    const refresh = () => {
      loadIncidents(token, incidentFilterRef.current);
      loadAuditLog(token);
    };
    source.addEventListener("incident.created", refresh);
    source.addEventListener("incident.updated", refresh);
    source.addEventListener("incident.escalated", refresh);
    return () => source.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  async function handleLogin(e: FormEvent) {
    e.preventDefault();
    setLoginError(null);
    const r = await fetch(`${API}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: loginEmail, password: loginPassword }),
    });
    if (!r.ok) {
      setLoginError("Invalid email or password.");
      return;
    }
    const body = await r.json();
    setToken(body.access_token as string);
  }

  async function changeMode(next: Mode) {
    if (!token || modeBusy) return;
    setModeBusy(true);
    setModeError(null);
    try {
      const r = await fetch(`${API}/api/v1/security/mode`, {
        method: "PUT",
        headers: authHeaders(token),
        body: JSON.stringify({ mode: next }),
      });
      if (!r.ok) {
        setModeError("Could not change the arming mode.");
        return;
      }
      setModeState(await r.json());
    } finally {
      setModeBusy(false);
    }
  }

  const openCount = useMemo(
    () => (incidents ? incidents.filter((incident) => incident.status === "open").length : 0),
    [incidents],
  );
  const cameraHealthIncidents = useMemo(
    () => (incidents || []).filter((incident) => incident.kind.startsWith("camera_") && incident.status !== "resolved"),
    [incidents],
  );

  if (!token) {
    return (
      <section className="panel admin-panel">
        <h3>Security sign-in</h3>
        <p className="muted">Sign in to view arming modes, incidents and the audit trail.</p>
        <form onSubmit={handleLogin} className="admin-form">
          <label>
            Email
            <input type="email" required value={loginEmail} onChange={(e) => setLoginEmail(e.target.value)} />
          </label>
          <label>
            Password
            <input type="password" required value={loginPassword} onChange={(e) => setLoginPassword(e.target.value)} />
          </label>
          <div className="admin-actions">
            <button type="submit">Sign in</button>
          </div>
          {loginError && <p className="error">{loginError}</p>}
        </form>
      </section>
    );
  }

  return (
    <>
      <section className="panel admin-panel">
        <h3>Arming mode</h3>
        <p className="muted">
          Disarmed and Home never raise an incident for indoor/unzoned activity. Away and Night arm every zone. This
          is a deterministic rule - it never depends on AI confidence.
        </p>
        <div className="mode-switcher" role="group" aria-label="Arming mode">
          {MODES.map((option) => (
            <button
              type="button"
              key={option.value}
              className={`mode-btn${mode?.mode === option.value ? " active" : ""}`}
              aria-pressed={mode?.mode === option.value}
              disabled={modeBusy}
              onClick={() => changeMode(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
        {mode && <p className="muted mode-hint">{MODES.find((option) => option.value === mode.mode)?.hint}</p>}
        {mode?.changed_at && <p className="muted mode-changed">Last changed {timeAgo(mode.changed_at)}.</p>}
        {modeError && <p className="error">{modeError}</p>}
        {cameraHealthIncidents.length > 0 && (
          <p className="error camera-health-banner">
            {cameraHealthIncidents.length} camera{cameraHealthIncidents.length === 1 ? "" : "s"} need attention:{" "}
            {cameraHealthIncidents.map((incident) => `${cameraName(incident.camera_id)} (${KIND_LABELS[incident.kind] || incident.kind})`).join(", ")}
          </p>
        )}
      </section>

      <SearchCard token={token} />
      <DigestCard token={token} />

      <section className="panel admin-panel incidents-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">SECURITY</span><h3>Incidents</h3></div>
          <span className="result-count" aria-live="polite">{openCount} open</span>
        </div>
        <div className="filter-group" role="group" aria-label="Incident filter">
          <button type="button" className={incidentFilter === "open" ? "active" : ""} aria-pressed={incidentFilter === "open"} onClick={() => setIncidentFilter("open")}>Open</button>
          <button type="button" className={incidentFilter === "all" ? "active" : ""} aria-pressed={incidentFilter === "all"} onClick={() => setIncidentFilter("all")}>All</button>
        </div>
        {incidentsError && <p className="error">{incidentsError}</p>}
        {incidents && incidents.length === 0 && (
          <div className="empty-state compact"><strong>No incidents</strong><p>{incidentFilter === "open" ? "Nothing needs attention right now." : "No incidents recorded yet."}</p></div>
        )}
        {incidents?.map((incident) => (
          <IncidentCard
            key={incident.id}
            incident={incident}
            token={token}
            cameraName={cameraName(incident.camera_id)}
            onChanged={() => loadIncidents(token, incidentFilter)}
          />
        ))}
      </section>

      <section className="panel admin-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">AUDIT TRAIL</span><h3>Recent security actions</h3></div>
        </div>
        {!auditLog?.length && <p className="muted">No audit entries yet.</p>}
        {!!auditLog?.length && (
          <ul className="audit-list">
            {auditLog.map((entry) => (
              <li key={entry.id} className="audit-row">
                <span className="audit-action">{entry.action}</span>
                <span className="muted">{entry.target_type ? `${entry.target_type}${entry.target_id ? ` · ${entry.target_id.slice(0, 8)}` : ""}` : ""}</span>
                <small>{timeAgo(entry.created_at)}</small>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
