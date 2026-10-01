"use client";
import { useCallback, useEffect, useState, type FormEvent } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type ChannelType = "webpush" | "ntfy" | "telegram" | "webhook";

type Channel = {
  id: string;
  type: ChannelType;
  name: string;
  enabled: boolean;
  min_severity: string;
  attach_images: boolean;
  config: Record<string, string>;
  has_secret: boolean;
  last_status: string | null;
  last_message: string | null;
  last_sent_at: string | null;
};

type Settings = {
  enabled: boolean;
  quiet_hours_enabled: boolean;
  quiet_hours_start: string;
  quiet_hours_end: string;
  quiet_hours_override_severity: string;
  min_severity: string;
  max_per_hour: number;
};

type Status = {
  notifications_enabled: boolean;
  web_push_available: boolean;
  web_push_detail: string;
  vapid_public_key: string | null;
  deep_links_configured: boolean;
  channel_count: number;
  enabled_channel_count: number;
  subscription_count: number;
};

const SEVERITIES = ["low", "medium", "high", "critical"];

const TYPE_LABELS: Record<ChannelType, string> = {
  webpush: "Web push (this browser)",
  ntfy: "ntfy",
  telegram: "Telegram",
  webhook: "Webhook",
};

/** What each channel needs, and what it is allowed to carry. ntfy and
 * Telegram can attach a snapshot because delivery is authenticated; web
 * push and webhooks never do. */
const TYPE_HINTS: Record<ChannelType, string> = {
  webpush: "Alerts to browsers that have subscribed below. Text and a link only - snapshots stay behind sign-in.",
  ntfy: "Publishes to an ntfy topic. Add an access token to keep the topic private; snapshots need one.",
  telegram: "Sends via a Telegram bot. Needs the bot token (stored encrypted) and the destination chat id.",
  webhook: "POSTs JSON to an HTTPS URL you control. A shared secret signs the body (X-HomeCam-Signature).",
};

function authHeaders(token: string | null): Record<string, string> {
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    "Content-Type": "application/json",
    "X-HomeCam-Request": "1",
  };
}

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  const output = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
  return output;
}

/** Subscribe/unsubscribe this browser for web push.
 *
 * Permission is requested only on an explicit click: a prompt nobody asked
 * for is the fastest way to get alerts blocked forever. */
function PushSubscribeCard({
  token,
  status,
  onChanged,
}: {
  token: string | null;
  status: Status | null;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [subscribed, setSubscribed] = useState<boolean | null>(null);

  const refresh = useCallback(async () => {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
      setSubscribed(false);
      return;
    }
    const registration = await navigator.serviceWorker.getRegistration();
    const existing = await registration?.pushManager?.getSubscription();
    setSubscribed(Boolean(existing));
  }, []);

  useEffect(() => {
    refresh().catch(() => setSubscribed(false));
  }, [refresh]);

  const supported =
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window;

  async function subscribe() {
    if (!status?.vapid_public_key) return;
    setBusy(true);
    setMessage(null);
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        setMessage("Notifications are blocked for this site. Allow them in your browser settings and try again.");
        return;
      }
      const registration = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(status.vapid_public_key),
      });
      const json = subscription.toJSON() as { endpoint?: string; keys?: { p256dh?: string; auth?: string } };
      const response = await fetch(`${API}/api/v1/notifications/push/subscriptions`, {
        method: "POST",
        headers: authHeaders(token),
        credentials: "include",
        body: JSON.stringify({
          endpoint: json.endpoint,
          p256dh: json.keys?.p256dh,
          auth: json.keys?.auth,
        }),
      });
      if (!response.ok) {
        setMessage("Could not register this browser for alerts.");
        return;
      }
      setMessage("This browser will now receive incident alerts.");
      setSubscribed(true);
      onChanged();
    } catch {
      setMessage("Could not enable alerts in this browser.");
    } finally {
      setBusy(false);
    }
  }

  async function unsubscribe() {
    setBusy(true);
    setMessage(null);
    try {
      const registration = await navigator.serviceWorker.getRegistration();
      const subscription = await registration?.pushManager?.getSubscription();
      if (subscription) {
        await fetch(`${API}/api/v1/notifications/push/subscriptions/remove`, {
          method: "POST",
          headers: authHeaders(token),
          credentials: "include",
          body: JSON.stringify({ endpoint: subscription.endpoint }),
        });
        await subscription.unsubscribe();
      }
      setSubscribed(false);
      setMessage("This browser will no longer receive alerts.");
      onChanged();
    } catch {
      setMessage("Could not turn alerts off in this browser.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="push-subscribe">
      <h4>This browser</h4>
      {!supported && <p className="muted">This browser does not support web push. Use ntfy or Telegram instead.</p>}
      {supported && status && !status.web_push_available && (
        <p className="muted">Web push is not configured on the server ({status.web_push_detail}).</p>
      )}
      {supported && status?.web_push_available && (
        <div className="admin-actions">
          {subscribed ? (
            <button type="button" onClick={unsubscribe} disabled={busy}>
              Turn off alerts here
            </button>
          ) : (
            <button type="button" onClick={subscribe} disabled={busy}>
              Enable alerts in this browser
            </button>
          )}
          <span className="muted">{status.subscription_count} device(s) registered</span>
        </div>
      )}
      {message && <p className="muted">{message}</p>}
      <p className="muted">
        Alerts show the camera, what was detected and when, and open the app when tapped. Snapshots are never
        included - they stay behind sign-in.
      </p>
    </div>
  );
}

function ChannelRow({
  channel,
  token,
  onChanged,
}: {
  channel: Channel;
  token: string | null;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  async function patch(body: Record<string, unknown>) {
    setBusy(true);
    try {
      await fetch(`${API}/api/v1/notifications/channels/${channel.id}`, {
        method: "PATCH",
        headers: authHeaders(token),
        credentials: "include",
        body: JSON.stringify(body),
      });
    } catch {
      setResult("Could not reach the server.");
    } finally {
      setBusy(false);
    }
    onChanged();
  }

  async function test() {
    setBusy(true);
    setResult(null);
    let response: Response;
    try {
      response = await fetch(`${API}/api/v1/notifications/channels/${channel.id}/test`, {
        method: "POST",
        headers: authHeaders(token),
        credentials: "include",
      });
    } catch {
      setResult("Could not reach the server.");
      return;
    } finally {
      setBusy(false);
    }
    if (!response.ok) {
      setResult("Test failed.");
      return;
    }
    const body = (await response.json()) as { status: string; detail: string | null };
    setResult(body.status === "sent" ? "Test sent." : `Test ${body.status}: ${body.detail || "no detail"}`);
    onChanged();
  }

  async function remove() {
    setBusy(true);
    try {
      await fetch(`${API}/api/v1/notifications/channels/${channel.id}`, {
        method: "DELETE",
        headers: authHeaders(token),
        credentials: "include",
      });
    } catch {
      setResult("Could not reach the server.");
    } finally {
      setBusy(false);
    }
    onChanged();
  }

  const imagesAllowed = channel.type === "telegram" || channel.type === "ntfy";

  return (
    <li className="channel-row">
      <div className="channel-head">
        <strong>{channel.name}</strong>
        <span className="muted">{TYPE_LABELS[channel.type]}</span>
        {channel.last_status && (
          <span className={channel.last_status === "sent" ? "success" : "error"}>{channel.last_status}</span>
        )}
      </div>
      <div className="channel-controls">
        <label>
          <input
            type="checkbox"
            checked={channel.enabled}
            disabled={busy}
            onChange={(e) => patch({ enabled: e.target.checked })}
          />
          Enabled
        </label>
        <label>
          Alert at or above
          <select
            value={channel.min_severity}
            disabled={busy}
            onChange={(e) => patch({ min_severity: e.target.value })}
          >
            {SEVERITIES.map((severity) => (
              <option key={severity} value={severity}>
                {severity}
              </option>
            ))}
          </select>
        </label>
        {imagesAllowed && (
          <label title="Only channels with authenticated delivery can carry a snapshot.">
            <input
              type="checkbox"
              checked={channel.attach_images}
              disabled={busy}
              onChange={(e) => patch({ attach_images: e.target.checked })}
            />
            Attach snapshot
          </label>
        )}
        <button type="button" onClick={test} disabled={busy}>
          Send test
        </button>
        <button type="button" onClick={remove} disabled={busy}>
          Remove
        </button>
      </div>
      {result && <p className="muted">{result}</p>}
      {channel.last_message && channel.last_status !== "sent" && <p className="error">{channel.last_message}</p>}
    </li>
  );
}

function AddChannelForm({ token, onAdded }: { token: string | null; onAdded: () => void }) {
  const [type, setType] = useState<ChannelType>("ntfy");
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const [topic, setTopic] = useState("");
  const [server, setServer] = useState("https://ntfy.sh");
  const [chatId, setChatId] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const config: Record<string, string> =
      type === "ntfy"
        ? { topic, server }
        : type === "telegram"
          ? { chat_id: chatId }
          : type === "webhook"
            ? { url }
            : {};
    const response = await fetch(`${API}/api/v1/notifications/channels`, {
      method: "POST",
      headers: authHeaders(token),
      credentials: "include",
      body: JSON.stringify({ type, name: name || TYPE_LABELS[type], config, secret: secret || null }),
    });
    setBusy(false);
    if (!response.ok) {
      const body = await response.json().catch(() => ({ detail: "Could not add channel." }));
      setError(typeof body.detail === "string" ? body.detail : "Could not add channel.");
      return;
    }
    setName("");
    setSecret("");
    setTopic("");
    setChatId("");
    setUrl("");
    onAdded();
  }

  return (
    <form className="admin-form" onSubmit={submit}>
      <h4>Add a channel</h4>
      <label>
        Type
        <select value={type} onChange={(e) => setType(e.target.value as ChannelType)}>
          {(Object.keys(TYPE_LABELS) as ChannelType[]).map((value) => (
            <option key={value} value={value}>
              {TYPE_LABELS[value]}
            </option>
          ))}
        </select>
      </label>
      <p className="muted">{TYPE_HINTS[type]}</p>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder={TYPE_LABELS[type]} />
      </label>
      {type === "ntfy" && (
        <>
          <label>
            Topic
            <input required value={topic} onChange={(e) => setTopic(e.target.value)} />
          </label>
          <label>
            Server
            <input value={server} onChange={(e) => setServer(e.target.value)} />
          </label>
          <label>
            Access token (optional)
            <input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} />
          </label>
        </>
      )}
      {type === "telegram" && (
        <>
          <label>
            Chat id
            <input required value={chatId} onChange={(e) => setChatId(e.target.value)} />
          </label>
          <label>
            Bot token
            <input type="password" required value={secret} onChange={(e) => setSecret(e.target.value)} />
          </label>
        </>
      )}
      {type === "webhook" && (
        <>
          <label>
            HTTPS URL
            <input required type="url" value={url} onChange={(e) => setUrl(e.target.value)} />
          </label>
          <label>
            Signing secret (optional)
            <input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} />
          </label>
        </>
      )}
      <div className="admin-actions">
        <button type="submit" disabled={busy}>
          Add channel
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <p className="muted">
        Secrets are stored encrypted and are never shown again, logged, or returned by the API.
      </p>
    </form>
  );
}

/** Instant incident alerts: who gets told, how loudly, and when not to.
 *
 * Everything here is opt-in and human-configured. No alert claims to know
 * who someone is - it reports what was detected, on which camera, when. */
export default function NotificationsPanel({ token }: { token: string | null }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [channels, setChannels] = useState<Channel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    try {
      const [statusRes, settingsRes, channelsRes] = await Promise.all([
        fetch(`${API}/api/v1/notifications/status`, { headers: authHeaders(token), credentials: "include" }),
        fetch(`${API}/api/v1/notifications/settings`, { headers: authHeaders(token), credentials: "include" }),
        fetch(`${API}/api/v1/notifications/channels`, { headers: authHeaders(token), credentials: "include" }),
      ]);
      if (!statusRes.ok || !settingsRes.ok || !channelsRes.ok) {
        setError("Could not load notification settings.");
        return;
      }
      setError(null);
      setStatus(await statusRes.json());
      setSettings(await settingsRes.json());
      setChannels(await channelsRes.json());
    } catch {
      setError("Could not load notification settings.");
    }
  }, [token]);

  useEffect(() => {
    load();
  }, [load]);

  async function saveSettings(next: Settings) {
    setSettings(next);
    const response = await fetch(`${API}/api/v1/notifications/settings`, {
      method: "PUT",
      headers: authHeaders(token),
      credentials: "include",
      body: JSON.stringify(next),
    });
    if (response.ok) {
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    }
  }

  return (
    <section className="panel admin-panel notifications-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">ALERTS</span>
          <h3>Instant incident alerts</h3>
        </div>
        {saved && <span className="success">Saved</span>}
      </div>
      <p className="muted">
        Send an alert when an incident is raised or escalates. Off until you add a channel. Alerts follow the arming
        mode: nothing is sent for activity that would not raise an incident.
      </p>
      {error && <p className="error">{error}</p>}

      {settings && (
        <div className="admin-form">
          <label>
            <input
              type="checkbox"
              checked={settings.enabled}
              onChange={(e) => saveSettings({ ...settings, enabled: e.target.checked })}
            />
            Send alerts
          </label>
          <label>
            Minimum severity
            <select
              value={settings.min_severity}
              onChange={(e) => saveSettings({ ...settings, min_severity: e.target.value })}
            >
              {SEVERITIES.map((severity) => (
                <option key={severity} value={severity}>
                  {severity}
                </option>
              ))}
            </select>
          </label>
          <label>
            Maximum alerts per hour, per channel
            <input
              type="number"
              min={1}
              max={200}
              value={settings.max_per_hour}
              onChange={(e) => saveSettings({ ...settings, max_per_hour: Number(e.target.value) })}
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={settings.quiet_hours_enabled}
              onChange={(e) => saveSettings({ ...settings, quiet_hours_enabled: e.target.checked })}
            />
            Quiet hours
          </label>
          {settings.quiet_hours_enabled && (
            <div className="quiet-hours">
              <label>
                From
                <input
                  type="time"
                  value={settings.quiet_hours_start}
                  onChange={(e) => saveSettings({ ...settings, quiet_hours_start: e.target.value })}
                />
              </label>
              <label>
                To
                <input
                  type="time"
                  value={settings.quiet_hours_end}
                  onChange={(e) => saveSettings({ ...settings, quiet_hours_end: e.target.value })}
                />
              </label>
              <label>
                Always alert at or above
                <select
                  value={settings.quiet_hours_override_severity}
                  onChange={(e) => saveSettings({ ...settings, quiet_hours_override_severity: e.target.value })}
                >
                  {SEVERITIES.map((severity) => (
                    <option key={severity} value={severity}>
                      {severity}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}
        </div>
      )}

      <PushSubscribeCard token={token} status={status} onChanged={load} />

      <h4>Channels</h4>
      {channels && channels.length === 0 && <p className="muted">No channels yet. Add one below.</p>}
      {!!channels?.length && (
        <ul className="channel-list">
          {channels.map((channel) => (
            <ChannelRow key={channel.id} channel={channel} token={token} onChanged={load} />
          ))}
        </ul>
      )}
      <AddChannelForm token={token} onAdded={load} />
      {status && !status.deep_links_configured && (
        <p className="muted">
          Set WEB_APP_BASE_URL on the API to include a link back to the incident in each alert.
        </p>
      )}
    </section>
  );
}
