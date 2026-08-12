#!/usr/bin/env python3
import hashlib
import hmac
import html
import http.cookies
import http.server
import ipaddress
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request


API_URL = os.environ.get("CROWDSEC_API_URL", "http://127.0.0.1:8080").rstrip("/")
CREDENTIALS_PATH = os.environ.get(
    "CROWDSEC_CREDENTIALS", "/run/crowdsec/local_api_credentials.yaml"
)
UI_PASSWORD = os.environ.get("CROWDSEC_UI_PASSWORD", "")
BIND = os.environ.get("CROWDSEC_UI_BIND", "0.0.0.0")
PORT = int(os.environ.get("CROWDSEC_UI_PORT", "8088"))

TOKEN = None
TOKEN_EXPIRES_AT = 0


def read_credentials():
    values = {}
    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            if ":" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def api_request(path, method="GET", payload=None):
    token = get_token()
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        f"{API_URL}{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 401:
            refresh_token()
            return api_request(path, method, payload)
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"CrowdSec API returned HTTP {error.code}: {detail}")

    if not data:
        return None
    return json.loads(data.decode("utf-8"))


def get_token():
    global TOKEN
    if TOKEN and time.time() < TOKEN_EXPIRES_AT:
        return TOKEN
    return refresh_token()


def refresh_token():
    global TOKEN, TOKEN_EXPIRES_AT
    credentials = read_credentials()
    payload = {
        "machine_id": credentials["login"],
        "password": credentials["password"],
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{API_URL}/v1/watchers/login",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
    TOKEN = data["token"]
    TOKEN_EXPIRES_AT = time.time() + 50 * 60
    return TOKEN


def active_bans():
    alerts = api_request(
        "/v1/alerts?has_active_decision=true&include_capi=false&limit=100"
    )
    bans = []
    for alert in alerts or []:
        source = alert.get("source") or {}
        meta = {
            item.get("key"): item.get("value")
            for item in alert.get("meta", [])
            if item.get("key")
        }
        for decision in alert.get("decisions") or []:
            if decision.get("type") != "ban":
                continue
            bans.append(
                {
                    "ip": decision.get("value", ""),
                    "country": source.get("cn", ""),
                    "as_name": source.get("as_name", ""),
                    "as_number": source.get("as_number", ""),
                    "reason": decision.get("scenario") or alert.get("scenario", ""),
                    "expires": decision.get("duration", ""),
                    "alert_id": alert.get("id", ""),
                    "events": alert.get("events_count", 0),
                    "target": trim_json_list(meta.get("target_uri", "")),
                    "method": trim_json_list(meta.get("method", "")),
                    "status": trim_json_list(meta.get("status", "")),
                    "user_agent": trim_json_list(meta.get("user_agent", "")),
                    "created_at": alert.get("created_at", ""),
                }
            )
    return bans


def trim_json_list(value):
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not parsed:
        return ""
    first = str(parsed[0])
    if len(parsed) == 1:
        return first
    return f"{first} (+{len(parsed) - 1})"


def delete_ban(ip):
    ipaddress.ip_address(ip)
    encoded = urllib.parse.urlencode({"ip": ip})
    return api_request(f"/v1/decisions?{encoded}", method="DELETE")


def session_value():
    if not UI_PASSWORD:
        return ""
    return hmac.new(
        UI_PASSWORD.encode("utf-8"), b"crowdsec-ui-session", hashlib.sha256
    ).hexdigest()


def csrf_value():
    if not UI_PASSWORD:
        return ""
    return hmac.new(
        UI_PASSWORD.encode("utf-8"), b"crowdsec-ui-csrf", hashlib.sha256
    ).hexdigest()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "CrowdSecUI/1.0"

    def do_GET(self):
        if self.path == "/health":
            self.send_json({"ok": True})
            return
        if self.path == "/login":
            self.send_html(login_page())
            return
        if self.path == "/api/bans":
            if not self.authorized():
                self.send_json({"error": "unauthorized"}, status=401)
                return
            self.send_json({"bans": active_bans(), "updated_at": int(time.time())})
            return
        if self.path == "/" or self.path == "/index.html":
            if not self.authorized():
                self.redirect("/login")
                return
            self.send_html(index_page(csrf_value()))
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/login":
            self.handle_login()
            return
        if self.path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header(
                "Set-Cookie",
                "crowdsec_ui_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
            )
            self.end_headers()
            return
        if self.path.startswith("/api/unban/"):
            if not self.authorized():
                self.send_json({"error": "unauthorized"}, status=401)
                return
            if UI_PASSWORD and self.headers.get("X-CSRF-Token") != csrf_value():
                self.send_json({"error": "bad csrf token"}, status=403)
                return
            ip = urllib.parse.unquote(self.path.rsplit("/", 1)[-1])
            try:
                delete_ban(ip)
            except ValueError:
                self.send_json({"error": "invalid ip"}, status=400)
                return
            except Exception as error:
                self.send_json({"error": str(error)}, status=502)
                return
            self.send_json({"ok": True, "ip": ip})
            return
        self.send_error(404)

    def handle_login(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        form = urllib.parse.parse_qs(data)
        password = form.get("password", [""])[0]
        if not UI_PASSWORD or secrets.compare_digest(password, UI_PASSWORD):
            self.send_response(303)
            self.send_header("Location", "/")
            if UI_PASSWORD:
                self.send_header(
                    "Set-Cookie",
                    "crowdsec_ui_session="
                    + session_value()
                    + "; Path=/; HttpOnly; SameSite=Lax",
                )
            self.end_headers()
            return
        self.send_html(login_page("Invalid password"), status=401)

    def authorized(self):
        if not UI_PASSWORD:
            return True
        cookies = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        value = cookies.get("crowdsec_ui_session")
        if not value:
            return False
        return secrets.compare_digest(value.value, session_value())

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body, status=200):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def login_page(error=""):
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    disabled_note = ""
    if not UI_PASSWORD:
        disabled_note = '<p class="note">No UI password is configured.</p>'
    return (
        BASE_HEAD
        + f"""
<body class="login-screen">
  <main class="login-panel">
    <h1>CrowdSec</h1>
    <form method="post" action="/login">
      {error_html}
      {disabled_note}
      <label>
        Password
        <input name="password" type="password" autocomplete="current-password" autofocus>
      </label>
      <button type="submit">Open</button>
    </form>
  </main>
</body>
</html>
"""
    )


def index_page(csrf):
    return (
        BASE_HEAD
        + f"""
<body>
  <header class="topbar">
    <div>
      <h1>CrowdSec</h1>
      <p id="summary">Loading bans</p>
    </div>
    <div class="actions">
      <button id="refresh" type="button">Refresh</button>
      <form method="post" action="/logout">
        <button type="submit" class="secondary">Lock</button>
      </form>
    </div>
  </header>

  <main>
    <section id="status" class="status">Loading...</section>
    <section id="bans" class="ban-list" aria-live="polite"></section>
  </main>

  <dialog id="confirm">
    <form method="dialog">
      <h2>Unban IP</h2>
      <p id="confirm-copy"></p>
      <menu>
        <button value="cancel" class="secondary">Cancel</button>
        <button id="confirm-unban" value="default">Unban</button>
      </menu>
    </form>
  </dialog>

  <script>
    window.CSRF_TOKEN = {json.dumps(csrf)};
  </script>
  <script>
{CLIENT_JS}
  </script>
</body>
</html>
"""
    )


BASE_HEAD = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CrowdSec Bans</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #15181d;
      --muted: #68707d;
      --line: #d9dee7;
      --accent: #1b6f9b;
      --danger: #b3261e;
      --danger-bg: #fff0ee;
      --ok-bg: #edf7ef;
      --ok: #176b3a;
      --shadow: 0 8px 30px rgba(18, 24, 38, 0.08);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #121418;
        --panel: #1a1e25;
        --text: #f1f4f8;
        --muted: #a8b0bd;
        --line: #303744;
        --accent: #79b8d8;
        --danger: #ffb4ab;
        --danger-bg: #3a1d1b;
        --ok-bg: #14311f;
        --ok: #a7d9b5;
        --shadow: none;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 20px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    h1, h2, p { margin: 0; }
    h1 { font-size: 22px; line-height: 1.15; }
    h2 { font-size: 18px; }
    #summary {
      color: var(--muted);
      margin-top: 4px;
      font-size: 14px;
    }
    main {
      width: min(1180px, 100%);
      margin: 0 auto;
      padding: 16px;
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    button {
      appearance: none;
      border: 1px solid var(--danger);
      background: var(--danger);
      color: white;
      border-radius: 7px;
      min-height: 42px;
      padding: 0 14px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      white-space: nowrap;
    }
    button.secondary {
      color: var(--text);
      background: transparent;
      border-color: var(--line);
    }
    button:disabled {
      opacity: 0.55;
      cursor: progress;
    }
    .status {
      min-height: 42px;
      display: flex;
      align-items: center;
      color: var(--muted);
      font-size: 14px;
    }
    .ban-list {
      display: grid;
      gap: 10px;
    }
    .ban {
      display: grid;
      grid-template-columns: minmax(130px, 1fr) 70px 100px minmax(160px, 1.3fr) minmax(160px, 1fr) 88px;
      gap: 12px;
      align-items: center;
      padding: 12px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .ip {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 15px;
      font-weight: 700;
    }
    .label {
      display: none;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 3px;
    }
    .value {
      min-width: 0;
      overflow-wrap: anywhere;
      line-height: 1.25;
    }
    .muted {
      color: var(--muted);
      font-size: 13px;
      margin-top: 3px;
    }
    .country {
      font-weight: 800;
      color: var(--accent);
    }
    .empty {
      padding: 24px;
      border: 1px solid var(--line);
      background: var(--ok-bg);
      color: var(--ok);
      border-radius: 8px;
      font-weight: 700;
      text-align: center;
    }
    dialog {
      width: min(420px, calc(100vw - 32px));
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--text);
      padding: 18px;
    }
    dialog::backdrop {
      background: rgba(0, 0, 0, 0.42);
    }
    dialog p {
      margin: 12px 0 18px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    menu {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      padding: 0;
      margin: 0;
    }
    .login-screen {
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 20px;
    }
    .login-panel {
      width: min(360px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
      box-shadow: var(--shadow);
    }
    label {
      display: grid;
      gap: 7px;
      margin: 16px 0;
      color: var(--muted);
      font-size: 14px;
      font-weight: 700;
    }
    input {
      width: 100%;
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 10px;
      background: var(--bg);
      color: var(--text);
      font: inherit;
    }
    .error {
      margin-top: 12px;
      color: var(--danger);
      font-weight: 700;
    }
    .note {
      margin-top: 12px;
      color: var(--muted);
    }
    @media (max-width: 760px) {
      .topbar {
        align-items: flex-start;
        padding: 14px;
      }
      main {
        padding: 12px;
      }
      .ban {
        grid-template-columns: 1fr;
        gap: 10px;
        padding: 14px;
      }
      .label {
        display: block;
      }
      .ban button {
        width: 100%;
      }
      .actions {
        flex-direction: column;
        align-items: stretch;
      }
      .actions button {
        width: 100%;
      }
    }
  </style>
</head>
"""


CLIENT_JS = r"""
const bansEl = document.querySelector("#bans");
const statusEl = document.querySelector("#status");
const summaryEl = document.querySelector("#summary");
const refreshButton = document.querySelector("#refresh");
const dialog = document.querySelector("#confirm");
const confirmCopy = document.querySelector("#confirm-copy");
const confirmButton = document.querySelector("#confirm-unban");
let selectedIp = null;
let loading = false;

function field(label, value, className = "") {
  const safe = value || "-";
  return `<div class="${className}"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(safe)}</div></div>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

function render(data) {
  const bans = data.bans || [];
  summaryEl.textContent = `${bans.length} active ban${bans.length === 1 ? "" : "s"}`;
  statusEl.textContent = `Updated ${new Date((data.updated_at || Date.now() / 1000) * 1000).toLocaleTimeString()}`;

  if (!bans.length) {
    bansEl.innerHTML = `<div class="empty">No active bans</div>`;
    return;
  }

  bansEl.innerHTML = bans.map((ban) => `
    <article class="ban">
      ${field("IP", ban.ip, "ip")}
      ${field("Country", ban.country, "country")}
      ${field("Expires", ban.expires)}
      ${field("Reason", ban.reason)}
      <div>
        <div class="label">Source</div>
        <div class="value">${escapeHtml(ban.as_name || "-")}</div>
        <div class="muted">${escapeHtml(ban.target || "")}</div>
      </div>
      <button type="button" data-ip="${escapeHtml(ban.ip)}">Unban</button>
    </article>
  `).join("");

  bansEl.querySelectorAll("button[data-ip]").forEach((button) => {
    button.addEventListener("click", () => openConfirm(button.dataset.ip));
  });
}

async function loadBans() {
  if (loading) return;
  loading = true;
  refreshButton.disabled = true;
  try {
    const response = await fetch("/api/bans", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    statusEl.textContent = `Failed to load bans: ${error.message}`;
  } finally {
    loading = false;
    refreshButton.disabled = false;
  }
}

function openConfirm(ip) {
  selectedIp = ip;
  confirmCopy.textContent = `Remove the active CrowdSec decision for ${ip}?`;
  dialog.showModal();
}

confirmButton.addEventListener("click", async (event) => {
  event.preventDefault();
  if (!selectedIp) return;
  confirmButton.disabled = true;
  try {
    const response = await fetch(`/api/unban/${encodeURIComponent(selectedIp)}`, {
      method: "POST",
      headers: { "X-CSRF-Token": window.CSRF_TOKEN || "" },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    dialog.close();
    selectedIp = null;
    await loadBans();
  } catch (error) {
    confirmCopy.textContent = `Unban failed: ${error.message}`;
  } finally {
    confirmButton.disabled = false;
  }
});

refreshButton.addEventListener("click", loadBans);
loadBans();
setInterval(loadBans, 15000);
"""


def main():
    server = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"crowdsec-ui listening on {BIND}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
