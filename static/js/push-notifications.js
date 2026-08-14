// static/js/push-notifications.js — Push opt-in + FCM token registration
// ═══════════════════════════════════════════════════════════════════════
// Shared by all three panels (admin, shopkeeper, customer). Each caller
// just does:
//     <script src="/static/js/push-notifications.js"></script>
//     <script>ClovicalPush.init('admin');</script>   // or 'shopkeeper' / 'customer'
//
// Deliberately does NOT call Notification.requestPermission() on its own —
// that's the auto-popup-on-page-load pattern users hate and browsers
// increasingly auto-block. Instead it shows a small dismissible in-page
// banner first; the native browser permission prompt only appears after
// the person clicks "Enable" on that banner.
//
// This file is pure addition: it touches no existing DOM, CSS, or globals
// other than defining `window.ClovicalPush`, and every step is wrapped so
// a failure (unsupported browser, missing Firebase config, network error)
// just silently no-ops instead of breaking the page it's included on.
// ═══════════════════════════════════════════════════════════════════════
(function () {
  const SW_URL = "/sw.js";
  const DISMISS_KEY = "clovical_push_prompt_dismissed";
  const FIREBASE_SDK = [
    "https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js",
    "https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js",
  ];

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const existing = document.querySelector(`script[src="${src}"]`);
      if (existing) {
        if (existing.dataset.loaded) return resolve();
        existing.addEventListener("load", () => resolve());
        existing.addEventListener("error", reject);
        return;
      }
      const script = document.createElement("script");
      script.src = src;
      script.onload = () => {
        script.dataset.loaded = "1";
        resolve();
      };
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  async function ensureFirebaseLoaded() {
    if (!window.firebase) {
      await loadScript(FIREBASE_SDK[0]);
      await loadScript(FIREBASE_SDK[1]);
    }
    if (!window.firebaseConfig) {
      await loadScript("/static/js/firebase-config.js");
    }
    return !!(window.firebaseConfig && window.firebaseConfig.apiKey && !window.firebaseConfig.apiKey.startsWith("REPLACE_WITH"));
  }

  function showEnableBanner(onEnable) {
    if (document.getElementById("clovical-push-banner")) return;
    const bar = document.createElement("div");
    bar.id = "clovical-push-banner";
    bar.setAttribute("role", "dialog");
    bar.style.cssText = [
      "position:fixed", "left:16px", "right:16px", "bottom:16px", "z-index:9999",
      "max-width:420px", "margin:0 auto", "background:#0A0A0A", "color:#fff",
      "padding:14px 16px", "border-radius:12px", "box-shadow:0 8px 30px rgba(0,0,0,.25)",
      "font-family:inherit", "font-size:0.85rem", "display:flex", "align-items:center",
      "gap:12px",
    ].join(";");
    bar.innerHTML = `
      <span style="flex:1;line-height:1.4">Get instant updates — turn on notifications for orders and alerts.</span>
      <button type="button" id="clovical-push-enable" style="background:#fff;color:#0A0A0A;border:none;border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer;white-space:nowrap">Enable</button>
      <button type="button" id="clovical-push-dismiss" aria-label="Dismiss" style="background:transparent;border:none;color:#bbb;font-size:1.1rem;cursor:pointer;padding:0 4px">✕</button>
    `;
    document.body.appendChild(bar);
    document.getElementById("clovical-push-enable").onclick = () => {
      bar.remove();
      onEnable();
    };
    document.getElementById("clovical-push-dismiss").onclick = () => {
      localStorage.setItem(DISMISS_KEY, "1");
      bar.remove();
    };
  }

  async function registerServiceWorkerAndGetToken() {
    const reg = await navigator.serviceWorker.register(SW_URL);
    if (!firebase.apps.length) firebase.initializeApp(window.firebaseConfig);
    const messaging = firebase.messaging();
    const token = await messaging.getToken({
      vapidKey: window.firebaseConfig.vapidKey,
      serviceWorkerRegistration: reg,
    });
    // Show a lightweight in-page toast for pushes that arrive while this
    // tab is focused (FCM does not auto-display those via the service
    // worker — only backgrounded/closed tabs go through onBackgroundMessage).
    messaging.onMessage((payload) => {
      const title = (payload.notification && payload.notification.title) || "";
      if (title && typeof window.showToast === "function") {
        window.showToast(title);
      }
    });
    return token;
  }

  async function sendTokenToServer(role, token) {
    const res = await fetch(`/api/notifications/${role}/register-token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ token }),
    });
    if (!res.ok) throw new Error(`register-token failed: ${res.status}`);
  }

  async function enableFor(role) {
    const ok = await ensureFirebaseLoaded();
    if (!ok) return; // firebase-config.js still has placeholder values — nothing to do yet
    const token = await registerServiceWorkerAndGetToken();
    if (token) await sendTokenToServer(role, token);
  }

  window.ClovicalPush = {
    init: async function (role) {
      try {
        if (!("Notification" in window) || !("serviceWorker" in navigator) || !("PushManager" in window)) return;
        if (Notification.permission === "denied") return;

        if (Notification.permission === "granted") {
          await enableFor(role);
          return;
        }

        if (localStorage.getItem(DISMISS_KEY) === "1") return;

        showEnableBanner(async () => {
          try {
            const perm = await Notification.requestPermission();
            if (perm === "granted") {
              await enableFor(role);
            } else {
              localStorage.setItem(DISMISS_KEY, "1");
            }
          } catch (e) {
            console.warn("Push enable failed:", e);
          }
        });
      } catch (e) {
        console.warn("Push init failed:", e);
      }
    },
  };
})();