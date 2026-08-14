// static/sw.js — Push notification service worker
// ═══════════════════════════════════════════════════════════════════════
// Served at root scope via GET /sw.js (see routers/public.py), which sends
// a `Service-Worker-Allowed: /` header so this file — even though it lives
// physically under static/ — can control the whole origin instead of just
// /static/*. Required for notificationclick to open any page on the site.
//
// This worker does two things:
//   1. Uses the Firebase Messaging compat SDK to receive background push
//      messages (tab closed / not focused) and display them as a native
//      OS notification.
//   2. Handles the click on that notification to focus an existing tab or
//      open a new one at the link the backend attached to the push.
//
// This is entirely new/additive — it does not touch, cache, or intercept
// any existing request on the site (no fetch handler), so it cannot affect
// checkout, payments, or any other existing functionality.
// ═══════════════════════════════════════════════════════════════════════

importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js");
importScripts("/static/js/firebase-config.js");

firebase.initializeApp(firebaseConfig);

// Only actually start listening if the placeholder config has been filled
// in — prevents noisy console errors on installs that haven't set up
// Firebase yet, without breaking anything else on the page.
const _configured = firebaseConfig.apiKey && !firebaseConfig.apiKey.startsWith("REPLACE_WITH");

if (_configured) {
  const messaging = firebase.messaging();

  // Fires when a push arrives while no tab has focus (or the site is
  // closed entirely). `payload.notification` is what we sent from
  // utils/fcm_push.py's "notification" field.
  messaging.onBackgroundMessage((payload) => {
    const title = (payload.notification && payload.notification.title) || "clovical";
    const body = (payload.notification && payload.notification.body) || "";
    const link = (payload.data && payload.data.link) || "/";
    self.registration.showNotification(title, {
      body,
      icon: "/static/images/favicon.svg",
      badge: "/static/images/favicon.svg",
      data: { link },
    });
  });
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const link = (event.notification.data && event.notification.data.link) || "/";
  const targetUrl = new URL(link, self.location.origin).href;

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        // Reuse an already-open tab on the same origin instead of always
        // opening a new one — focus it and navigate if the link differs.
        if (client.url && new URL(client.url).origin === self.location.origin && "focus" in client) {
          client.focus();
          if (client.url !== targetUrl && "navigate" in client) {
            return client.navigate(targetUrl);
          }
          return;
        }
      }
      return clients.openWindow(targetUrl);
    })
  );
});