"use client";
import { useEffect, useState, type FormEvent } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type ProviderConfig = {
  id: string;
  provider_type: string;
  name: string;
  enabled: boolean;
  mode?: string | null;
  scheme?: string | null;
  host?: string | null;
  port?: number | null;
  username?: string | null;
  channels?: string | null;
  adapter_url?: string | null;
  has_secret: boolean;
  last_test_status?: string | null;
  last_test_message?: string | null;
  last_test_at?: string | null;
};

type TestResult = { success: boolean; status: string; message: string };
type ChannelRow = { channel: string; name: string };
type CameraOption = { id: string; name: string };
type CameraZone = {
  id: string;
  camera_id: string;
  name: string;
  kind: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
};

/** Zone kinds the detection pipeline understands semantically
 * (see docs/ai-pipeline.md). Any other name is stored and shown, it simply
 * carries no extra meaning. */
const ZONE_KINDS = ["driveway", "parking", "mailbox", "entry", "street", "garden", "other"];

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

function channelsToRows(channels: string): ChannelRow[] {
  const rows = channels
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const [channel, name] = part.split(":");
      return { channel: channel ?? "", name: name ?? "" };
    });
  return rows.length ? rows : [{ channel: "1", name: "" }];
}

function rowsToChannels(rows: ChannelRow[]): string {
  return rows
    .filter((row) => row.channel.trim())
    .map((row) => (row.name.trim() ? `${row.channel.trim()}:${row.name.trim()}` : row.channel.trim()))
    .join(",");
}

/** Admin/Settings section: lets the signed-in user configure real Dahua and
 * Eufy connections at runtime instead of only via .env at process startup
 * (see docs/dahua.md and docs/eufy.md). The HomeCam session token is kept
 * only in component state (never localStorage) since this phase treats any
 * authenticated user as admin. */
export default function AdminPanel() {
  const [token, setToken] = useState<string | null>(null);
  const [loginEmail, setLoginEmail] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);

  const [configs, setConfigs] = useState<ProviderConfig[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [dahuaEditingId, setDahuaEditingId] = useState<string | null>(null);
  const [dahuaMode, setDahuaMode] = useState<"direct" | "edge">("direct");
  const [dahuaName, setDahuaName] = useState("Dahua NVR");
  const [dahuaScheme, setDahuaScheme] = useState("http");
  const [dahuaHost, setDahuaHost] = useState("");
  const [dahuaPort, setDahuaPort] = useState("80");
  const [dahuaUsername, setDahuaUsername] = useState("");
  const [dahuaPassword, setDahuaPassword] = useState("");
  const [dahuaChannels, setDahuaChannels] = useState<ChannelRow[]>([{ channel: "1", name: "Front Door" }]);
  const [dahuaEdgeBaseUrl, setDahuaEdgeBaseUrl] = useState("");
  const [dahuaEdgeToken, setDahuaEdgeToken] = useState("");
  const [dahuaStatus, setDahuaStatus] = useState<string | null>(null);
  const [dahuaTest, setDahuaTest] = useState<TestResult | null>(null);
  const [dahuaBusy, setDahuaBusy] = useState(false);

  const [eufyEditingId, setEufyEditingId] = useState<string | null>(null);
  const [eufyName, setEufyName] = useState("Eufy Adapter");
  const [eufyAdapterUrl, setEufyAdapterUrl] = useState("");
  const [eufyToken, setEufyToken] = useState("");
  const [eufyStatus, setEufyStatus] = useState<string | null>(null);
  const [eufyTest, setEufyTest] = useState<TestResult | null>(null);
  const [eufyBusy, setEufyBusy] = useState(false);

  const [cameras, setCameras] = useState<CameraOption[]>([]);
  const [zoneCameraId, setZoneCameraId] = useState("");
  const [zones, setZones] = useState<CameraZone[]>([]);
  const [zoneName, setZoneName] = useState("driveway");
  const [zoneKind, setZoneKind] = useState("driveway");
  const [zoneBox, setZoneBox] = useState({ x1: "0.0", y1: "0.5", x2: "0.6", y2: "1.0" });
  const [zoneStatus, setZoneStatus] = useState<string | null>(null);
  const [zoneBusy, setZoneBusy] = useState(false);

  async function loadConfigs(activeToken: string) {
    const r = await fetch(`${API}/api/v1/admin/providers`, { headers: authHeaders(activeToken) });
    if (r.status === 401) {
      setToken(null);
      return;
    }
    if (!r.ok) {
      setLoadError("Could not load configured providers.");
      return;
    }
    setLoadError(null);
    setConfigs(await r.json());
  }

  useEffect(() => {
    if (token) loadConfigs(token);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    (async () => {
      const r = await fetch(`${API}/api/v1/cameras`);
      if (!r.ok || cancelled) return;
      const body = (await r.json()) as CameraOption[];
      setCameras(body);
      setZoneCameraId((current) => current || body[0]?.id || "");
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  async function loadZones(cameraId: string, activeToken: string) {
    if (!cameraId) {
      setZones([]);
      return;
    }
    const r = await fetch(`${API}/api/v1/admin/cameras/${cameraId}/zones`, {
      headers: authHeaders(activeToken),
    });
    if (!r.ok) {
      setZones([]);
      return;
    }
    setZones(await r.json());
  }

  useEffect(() => {
    if (token && zoneCameraId) loadZones(zoneCameraId, token);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, zoneCameraId]);

  async function saveZone(e: FormEvent) {
    e.preventDefault();
    if (!token || !zoneCameraId) return;
    setZoneBusy(true);
    setZoneStatus(null);
    try {
      const r = await fetch(`${API}/api/v1/admin/cameras/${zoneCameraId}/zones`, {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify({
          name: zoneName,
          kind: zoneKind,
          x1: Number(zoneBox.x1),
          y1: Number(zoneBox.y1),
          x2: Number(zoneBox.x2),
          y2: Number(zoneBox.y2),
        }),
      });
      if (!r.ok) {
        setZoneStatus("Could not save the zone. Coordinates must be between 0 and 1, with x1 < x2 and y1 < y2.");
        return;
      }
      setZoneStatus("Zone saved.");
      await loadZones(zoneCameraId, token);
    } finally {
      setZoneBusy(false);
    }
  }

  async function removeZone(zone: CameraZone) {
    if (!token) return;
    await fetch(`${API}/api/v1/admin/cameras/${zone.camera_id}/zones/${zone.id}`, {
      method: "DELETE",
      headers: authHeaders(token),
    });
    await loadZones(zone.camera_id, token);
  }

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

  function resetDahuaForm() {
    setDahuaEditingId(null);
    setDahuaMode("direct");
    setDahuaName("Dahua NVR");
    setDahuaScheme("http");
    setDahuaHost("");
    setDahuaPort("80");
    setDahuaUsername("");
    setDahuaPassword("");
    setDahuaChannels([{ channel: "1", name: "Front Door" }]);
    setDahuaEdgeBaseUrl("");
    setDahuaEdgeToken("");
    setDahuaTest(null);
  }

  function editDahua(config: ProviderConfig) {
    setDahuaEditingId(config.id);
    setDahuaMode(config.mode === "edge" ? "edge" : "direct");
    setDahuaName(config.name);
    setDahuaScheme(config.scheme || "http");
    setDahuaHost(config.host || "");
    setDahuaPort(String(config.port ?? 80));
    setDahuaUsername(config.username || "");
    setDahuaPassword("");
    setDahuaChannels(channelsToRows(config.channels || ""));
    // The edge base URL is stored in the same column as the direct-mode
    // adapter_url is for Eufy; only meaningful when mode === "edge".
    setDahuaEdgeBaseUrl(config.mode === "edge" ? config.adapter_url || "" : "");
    setDahuaEdgeToken("");
    setDahuaTest(null);
    setDahuaStatus(null);
  }

  function updateChannelRow(index: number, field: keyof ChannelRow, value: string) {
    setDahuaChannels((rows) => rows.map((row, i) => (i === index ? { ...row, [field]: value } : row)));
  }

  function addChannelRow() {
    setDahuaChannels((rows) => [...rows, { channel: String(rows.length + 1), name: "" }]);
  }

  function removeChannelRow(index: number) {
    setDahuaChannels((rows) => (rows.length > 1 ? rows.filter((_, i) => i !== index) : rows));
  }

  function dahuaPayload(): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      name: dahuaName,
      mode: dahuaMode,
      enabled: true,
    };
    if (dahuaMode === "edge") {
      payload.edge_base_url = dahuaEdgeBaseUrl;
      if (dahuaEdgeToken) payload.edge_token = dahuaEdgeToken;
    } else {
      payload.scheme = dahuaScheme;
      payload.host = dahuaHost;
      payload.port = Number(dahuaPort) || 80;
      payload.username = dahuaUsername;
      payload.channels = rowsToChannels(dahuaChannels);
      if (dahuaPassword) payload.password = dahuaPassword;
    }
    return payload;
  }

  async function saveDahua(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setDahuaBusy(true);
    setDahuaStatus(null);
    try {
      const url = dahuaEditingId
        ? `${API}/api/v1/admin/providers/dahua/${dahuaEditingId}`
        : `${API}/api/v1/admin/providers/dahua`;
      const r = await fetch(url, {
        method: dahuaEditingId ? "PUT" : "POST",
        headers: authHeaders(token),
        body: JSON.stringify(dahuaPayload()),
      });
      if (!r.ok) {
        setDahuaStatus("Save failed. Check the connection details and try again.");
        return;
      }
      const body = await r.json();
      setDahuaEditingId(body.id);
      setDahuaPassword("");
      setDahuaStatus("Saved.");
      await loadConfigs(token);
    } finally {
      setDahuaBusy(false);
    }
  }

  async function testDahua() {
    if (!token) return;
    setDahuaBusy(true);
    setDahuaTest(null);
    try {
      let payload: Record<string, unknown>;
      if (dahuaEditingId) {
        payload = {
          config_id: dahuaEditingId,
          mode: dahuaMode,
          ...(dahuaMode === "edge"
            ? { edge_base_url: dahuaEdgeBaseUrl, ...(dahuaEdgeToken ? { edge_token: dahuaEdgeToken } : {}) }
            : { ...(dahuaPassword ? { password: dahuaPassword } : {}) }),
        };
      } else if (dahuaMode === "edge") {
        payload = { mode: "edge", edge_base_url: dahuaEdgeBaseUrl, edge_token: dahuaEdgeToken };
      } else {
        payload = {
          mode: "direct",
          scheme: dahuaScheme,
          host: dahuaHost,
          port: Number(dahuaPort) || 80,
          username: dahuaUsername,
          password: dahuaPassword,
          channels: rowsToChannels(dahuaChannels),
        };
      }
      const r = await fetch(`${API}/api/v1/admin/providers/dahua/test`, {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        setDahuaTest(await r.json());
        if (dahuaEditingId) await loadConfigs(token);
      } else {
        setDahuaTest({ success: false, status: "ERROR", message: "Test connection request failed." });
      }
    } finally {
      setDahuaBusy(false);
    }
  }

  function resetEufyForm() {
    setEufyEditingId(null);
    setEufyName("Eufy Adapter");
    setEufyAdapterUrl("");
    setEufyToken("");
    setEufyTest(null);
  }

  function editEufy(config: ProviderConfig) {
    setEufyEditingId(config.id);
    setEufyName(config.name);
    setEufyAdapterUrl(config.adapter_url || "");
    setEufyToken("");
    setEufyTest(null);
    setEufyStatus(null);
  }

  function eufyPayload(): Record<string, unknown> {
    const payload: Record<string, unknown> = { name: eufyName, adapter_url: eufyAdapterUrl, enabled: true };
    if (eufyToken) payload.adapter_token = eufyToken;
    return payload;
  }

  async function saveEufy(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setEufyBusy(true);
    setEufyStatus(null);
    try {
      const url = eufyEditingId
        ? `${API}/api/v1/admin/providers/eufy/${eufyEditingId}`
        : `${API}/api/v1/admin/providers/eufy`;
      const r = await fetch(url, {
        method: eufyEditingId ? "PUT" : "POST",
        headers: authHeaders(token),
        body: JSON.stringify(eufyPayload()),
      });
      if (!r.ok) {
        setEufyStatus("Save failed. Check the adapter URL and try again.");
        return;
      }
      const body = await r.json();
      setEufyEditingId(body.id);
      setEufyToken("");
      setEufyStatus("Saved.");
      await loadConfigs(token);
    } finally {
      setEufyBusy(false);
    }
  }

  async function testEufy() {
    if (!token) return;
    setEufyBusy(true);
    setEufyTest(null);
    try {
      const payload: Record<string, unknown> = eufyEditingId
        ? { config_id: eufyEditingId, ...(eufyToken ? { adapter_token: eufyToken } : {}) }
        : { adapter_url: eufyAdapterUrl, adapter_token: eufyToken };
      const r = await fetch(`${API}/api/v1/admin/providers/eufy/test`, {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        setEufyTest(await r.json());
        if (eufyEditingId) await loadConfigs(token);
      } else {
        setEufyTest({ success: false, status: "ERROR", message: "Test connection request failed." });
      }
    } finally {
      setEufyBusy(false);
    }
  }

  async function toggleEnabled(config: ProviderConfig) {
    if (!token) return;
    await fetch(`${API}/api/v1/admin/providers/${config.id}/enabled`, {
      method: "POST",
      headers: authHeaders(token),
      body: JSON.stringify({ enabled: !config.enabled }),
    });
    await loadConfigs(token);
  }

  async function removeConfig(config: ProviderConfig) {
    if (!token) return;
    await fetch(`${API}/api/v1/admin/providers/${config.id}`, { method: "DELETE", headers: authHeaders(token) });
    if (dahuaEditingId === config.id) resetDahuaForm();
    if (eufyEditingId === config.id) resetEufyForm();
    await loadConfigs(token);
  }

  if (!token) {
    return (
      <section className="panel admin-panel">
        <h3>Admin sign-in</h3>
        <p className="muted">
          Sign in to configure Dahua and Eufy connections. Any authenticated account is treated as admin in this
          local, single-user build.
        </p>
        <form onSubmit={handleLogin} className="admin-form">
          <label>
            Email
            <input type="email" required value={loginEmail} onChange={(e) => setLoginEmail(e.target.value)} />
          </label>
          <label>
            Password
            <input
              type="password"
              required
              value={loginPassword}
              onChange={(e) => setLoginPassword(e.target.value)}
            />
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
        <h3>Dahua NVR connection</h3>
        <form onSubmit={saveDahua} className="admin-form">
          <label>
            Label
            <input value={dahuaName} onChange={(e) => setDahuaName(e.target.value)} />
          </label>
          <label>
            Connection mode
            <select value={dahuaMode} onChange={(e) => setDahuaMode(e.target.value as "direct" | "edge")}>
              <option value="direct">Direct (LAN host/RTSP, needs private network access to the NVR)</option>
              <option value="edge">Home Assistant / Raspberry Pi edge connector (Tailscale, recommended)</option>
            </select>
          </label>
          {dahuaMode === "edge" ? (
            <>
              <p className="hint">
                Point this at the edge connector you deployed on Home Assistant/Raspberry Pi (see
                docs/edge-connector.md), reachable over Tailscale or another private VPN/overlay. HomeCam never
                sees your Dahua username/password in this mode.
              </p>
              <label>
                Edge connector base URL
                <input
                  value={dahuaEdgeBaseUrl}
                  onChange={(e) => setDahuaEdgeBaseUrl(e.target.value)}
                  placeholder="https://raspberrypi.your-tailnet.ts.net:8443"
                  required
                />
              </label>
              <label>
                Edge connector token
                <input
                  type="password"
                  value={dahuaEdgeToken}
                  onChange={(e) => setDahuaEdgeToken(e.target.value)}
                  placeholder={dahuaEditingId ? "Leave blank to keep the stored token" : ""}
                />
              </label>
            </>
          ) : (
            <>
              <label>
                Scheme
                <select value={dahuaScheme} onChange={(e) => setDahuaScheme(e.target.value)}>
                  <option value="http">http</option>
                  <option value="https">https</option>
                </select>
              </label>
              <label>
                Host
                <input
                  value={dahuaHost}
                  onChange={(e) => setDahuaHost(e.target.value)}
                  placeholder="192.168.x.x"
                  required
                />
              </label>
              <label>
                Port
                <input
                  type="number"
                  value={dahuaPort}
                  onChange={(e) => setDahuaPort(e.target.value)}
                  min={1}
                  max={65535}
                />
              </label>
              <label>
                Username
                <input value={dahuaUsername} onChange={(e) => setDahuaUsername(e.target.value)} required />
              </label>
              <label>
                Password
                <input
                  type="password"
                  value={dahuaPassword}
                  onChange={(e) => setDahuaPassword(e.target.value)}
                  placeholder={dahuaEditingId ? "Leave blank to keep the stored password" : ""}
                />
              </label>
              <fieldset className="channel-editor">
                <legend>Channels</legend>
                {dahuaChannels.map((row, index) => (
                  <div className="channel-row" key={index}>
                    <input
                      aria-label={`Channel ${index + 1} number`}
                      value={row.channel}
                      onChange={(e) => updateChannelRow(index, "channel", e.target.value)}
                    />
                    <input
                      aria-label={`Channel ${index + 1} name`}
                      value={row.name}
                      onChange={(e) => updateChannelRow(index, "name", e.target.value)}
                      placeholder="Front Door"
                    />
                    <button
                      type="button"
                      onClick={() => removeChannelRow(index)}
                      aria-label={`Remove channel ${index + 1}`}
                    >
                      Remove
                    </button>
                  </div>
                ))}
                <button type="button" onClick={addChannelRow}>
                  Add channel
                </button>
              </fieldset>
            </>
          )}
          <div className="admin-actions">
            <button type="submit" disabled={dahuaBusy}>
              Save
            </button>
            <button type="button" onClick={testDahua} disabled={dahuaBusy}>
              Test Connection
            </button>
            {dahuaEditingId && (
              <button type="button" onClick={resetDahuaForm}>
                New config
              </button>
            )}
          </div>
          {dahuaStatus && <p>{dahuaStatus}</p>}
          {dahuaTest && (
            <p className={dahuaTest.success ? "success" : "error"}>
              {dahuaTest.status}: {dahuaTest.message}
            </p>
          )}
        </form>
      </section>

      <section className="panel admin-panel">
        <h3>Eufy adapter connection</h3>
        <form onSubmit={saveEufy} className="admin-form">
          <label>
            Label
            <input value={eufyName} onChange={(e) => setEufyName(e.target.value)} />
          </label>
          <label>
            Adapter base URL
            <input
              value={eufyAdapterUrl}
              onChange={(e) => setEufyAdapterUrl(e.target.value)}
              placeholder="http://127.0.0.1:8090"
              required
            />
          </label>
          <label>
            Adapter token
            <input
              type="password"
              value={eufyToken}
              onChange={(e) => setEufyToken(e.target.value)}
              placeholder={eufyEditingId ? "Leave blank to keep the stored token" : "optional"}
            />
          </label>
          <div className="admin-actions">
            <button type="submit" disabled={eufyBusy}>
              Save
            </button>
            <button type="button" onClick={testEufy} disabled={eufyBusy}>
              Test Connection
            </button>
            {eufyEditingId && (
              <button type="button" onClick={resetEufyForm}>
                New config
              </button>
            )}
          </div>
          {eufyStatus && <p>{eufyStatus}</p>}
          {eufyTest && (
            <p className={eufyTest.success ? "success" : "error"}>
              {eufyTest.status}: {eufyTest.message}
            </p>
          )}
        </form>
      </section>

      <section className="panel admin-panel">
        <h3>Detection zones</h3>
        <p className="muted">
          Zones are labelled rectangles in normalized image coordinates (0–1, origin top-left). The AI pipeline uses
          them to turn raw detections into meaningful events: a person in a <code>driveway</code> zone, a car parked in
          a <code>parking</code> zone, or activity at the <code>mailbox</code>. See docs/ai-pipeline.md.
        </p>
        <form onSubmit={saveZone} className="admin-form">
          <label>
            Camera
            <select value={zoneCameraId} onChange={(e) => setZoneCameraId(e.target.value)}>
              {cameras.map((camera) => (
                <option key={camera.id} value={camera.id}>
                  {camera.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Zone name
            <input value={zoneName} onChange={(e) => setZoneName(e.target.value)} required />
          </label>
          <label>
            Zone kind
            <select value={zoneKind} onChange={(e) => setZoneKind(e.target.value)}>
              {ZONE_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {kind}
                </option>
              ))}
            </select>
          </label>
          <fieldset className="channel-editor">
            <legend>Rectangle (normalized 0–1)</legend>
            <div className="channel-row">
              {(["x1", "y1", "x2", "y2"] as const).map((field) => (
                <input
                  key={field}
                  aria-label={`Zone ${field}`}
                  type="number"
                  step="0.01"
                  min={0}
                  max={1}
                  value={zoneBox[field]}
                  onChange={(e) => setZoneBox((box) => ({ ...box, [field]: e.target.value }))}
                />
              ))}
            </div>
          </fieldset>
          <div className="admin-actions">
            <button type="submit" disabled={zoneBusy || !zoneCameraId}>
              Add zone
            </button>
          </div>
          {zoneStatus && <p>{zoneStatus}</p>}
        </form>
        {zones.length === 0 ? (
          <p className="muted">No zones configured for this camera yet.</p>
        ) : (
          <ul className="provider-list">
            {zones.map((zone) => (
              <li key={zone.id} className="provider-row">
                <span className="provider-type">{zone.kind}</span>
                <span className="provider-summary">
                  {zone.name} — [{zone.x1}, {zone.y1}] → [{zone.x2}, {zone.y2}]
                </span>
                <div className="provider-row-actions">
                  <button type="button" onClick={() => removeZone(zone)} aria-label={`Delete zone ${zone.name}`}>
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel admin-panel">
        <h3>Configured providers</h3>
        {loadError && <p className="error">{loadError}</p>}
        {configs.length === 0 && !loadError && <p className="muted">No providers configured yet.</p>}
        {configs.length > 0 && (
          <ul className="provider-list">
            {configs.map((config) => (
              <li key={config.id} className="provider-row">
                <span className="provider-type">
                  {config.provider_type}
                  {config.provider_type === "dahua" && config.mode ? ` (${config.mode})` : ""}
                </span>
                <span className="provider-summary">
                  {config.provider_type === "dahua"
                    ? config.mode === "edge"
                      ? config.adapter_url || "(no edge base URL set)"
                      : `${config.scheme}://${config.host}:${config.port}`
                    : config.adapter_url}
                </span>
                <span className={config.enabled ? "success" : "muted"}>{config.enabled ? "Enabled" : "Disabled"}</span>
                <span className="muted">
                  {config.last_test_status ? `Last test: ${config.last_test_status}` : "Not tested yet"}
                </span>
                <div className="provider-row-actions">
                  <button type="button" onClick={() => (config.provider_type === "dahua" ? editDahua(config) : editEufy(config))}>
                    Edit
                  </button>
                  <button type="button" onClick={() => toggleEnabled(config)}>
                    {config.enabled ? "Disable" : "Enable"}
                  </button>
                  <button type="button" onClick={() => removeConfig(config)}>
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
