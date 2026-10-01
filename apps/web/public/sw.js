/* Service worker for HomeCam AI incident alerts.
 *
 * Deliberately minimal: it shows the pushed text and opens the deep link.
 * Push payloads never contain imagery or any identity claim - a snapshot
 * stays behind the app's own authentication, which is exactly what you
 * want for something that renders on a lock screen.
 */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (err) {
    payload = { body: event.data ? event.data.text() : "New incident" };
  }
  const title = payload.title || "HomeCam AI";
  const options = {
    body: payload.body || "A new incident was recorded.",
    tag: payload.tag || "homecam-incident",
    // Re-alert rather than silently replacing: an escalation matters.
    renotify: Boolean(payload.tag),
    timestamp: payload.timestamp ? Date.parse(payload.timestamp) || Date.now() : Date.now(),
    data: { url: payload.url || "/" },
    icon: "/icon.svg",
    badge: "/icon.svg",
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      // Reuse an already-open tab when there is one, so clicking an alert
      // doesn't pile up windows on a phone.
      for (const client of windows) {
        if ("focus" in client) {
          if ("navigate" in client) client.navigate(target);
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});
