// static/js/firebase-config.js
// ═══════════════════════════════════════════════════════════════════════
// Public Firebase Web App config. Fill this in from:
//   Firebase Console → Project Settings → General → "Your apps" →
//   Web app → SDK setup and configuration → Config
//
// This object (apiKey, projectId, etc.) is NOT a secret — it's the same
// public config Firebase has every website embed client-side, scoped by
// Firebase's own security rules and (optionally) HTTP referrer restrictions
// on the API key in Google Cloud Console. Do not put the *service account*
// JSON here — that one stays server-side only (Railway env vars), never in
// this file or anywhere under static/.
//
// vapidKey is the "Web Push certificate" key pair from:
//   Firebase Console → Project Settings → Cloud Messaging → Web configuration
//   → Web Push certificates → generate/copy the key pair (the public key
//   string, starts with something like "BN...").
//
// Loaded by both static/sw.js (background push) and
// static/js/push-notifications.js (foreground token registration) — keep
// this one file as the single source of truth for both.
// ═══════════════════════════════════════════════════════════════════════
const firebaseConfig = {
  apiKey: "AIzaSyDcQVI1MpNiZWoOPVKatGZNu1O7l6LlYm0",
  authDomain: "clovical-1bae5.firebaseapp.com",
  projectId: "clovical-1bae5",
  storageBucket: "clovical-1bae5.firebasestorage.app",
  messagingSenderId: "459104198866",
  appId: "1:459104198866:web:bd3e648a34f66eba11cd0b",
  vapidKey: "BCgG0nC2s_H1yXKi0CSa2owfz1KHwlZotWJYCCbTLpb5H7lY7ivrxB84_oUecm4nFVCeXb44QSCc3itC0fMIjHU",
};

// Service workers can't use ES module `export` without extra config, and
// this same file is loaded both as a plain <script> (main thread) and via
// importScripts() (service worker) — a bare `const` in global scope works
// as a plain identifier in both contexts (sw.js and push-notifications.js's
// SW code both just reference `firebaseConfig` directly), BUT a top-level
// `const` does NOT attach itself to `window`/`self` as a property. Code
// that checks `window.firebaseConfig` (see push-notifications.js's
// ensureFirebaseLoaded()) needs it explicitly assigned as a property, not
// just declared. `self` exists in both the main thread and a service
// worker (aliasing `window` on the main thread), so this one line covers
// both without an environment check.
self.firebaseConfig = firebaseConfig;
   // apiKey: "AIzaSyDcQVI1MpNiZWoOPVKatGZNu1O7l6LlYm0",
   // authDomain: "clovical-1bae5.firebaseapp.com",
   // projectId: "clovical-1bae5",
   // storageBucket: "clovical-1bae5.firebasestorage.app",
   // messagingSenderId: "459104198866",
   // appId: "1:459104198866:web:bd3e648a34f66eba11cd0b",
   // measurementId: "G-0WR5GQXBKP"