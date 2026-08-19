"""
Single version string for static assets, generated once when the app
process starts. Appended as ?v=... to shared.js / shared.css references
in templates so a deploy always busts stale browser/webview caches —
this is what let a page load a fresh HTML template that calls a JS
function (e.g. escapeHtml) while an old cached shared.js without that
function was still being served, causing "escapeHtml is not defined".

Railway (and most PaaS deploys) restart the process on every deploy, so
tying the version to process start time is enough — no file hashing
needed, and it costs nothing at request time.
"""
import time

ASSET_VERSION = str(int(time.time()))