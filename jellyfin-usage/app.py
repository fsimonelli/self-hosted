import base64
import html
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse


JELLYFIN_DB = os.environ.get("JELLYFIN_DB", "/jellyfin-data/jellyfin.db")
PLAYBACK_DB = os.environ.get("PLAYBACK_REPORTING_DB", "/jellyfin-data/playback_reporting.db")
STATE_DB = os.environ.get("STATE_DB", "/data/jellyfin_usage.db")
ADMIN_PASSWORD = os.environ.get("JELLYFIN_USAGE_PASSWORD", "")
DEFAULT_FEE = os.environ.get("JELLYFIN_USAGE_MONTHLY_FEE", "0")
DEFAULT_CURRENCY = os.environ.get("JELLYFIN_USAGE_CURRENCY", "USD")
HISTORY_DAYS = int(os.environ.get("JELLYFIN_USAGE_HISTORY_DAYS", "90"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))


def connect_readonly(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def connect_state():
    os.makedirs(os.path.dirname(STATE_DB), exist_ok=True)
    con = sqlite3.connect(STATE_DB)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        create table if not exists fee_status (
            user_id text primary key,
            username text not null,
            expected_fee real,
            currency text not null,
            paid_through text,
            last_paid_on text,
            notes text,
            updated_at text not null
        )
        """
    )
    con.execute(
        """
        create table if not exists payment_events (
            id integer primary key autoincrement,
            user_id text not null,
            username text not null,
            amount real,
            currency text not null,
            paid_on text not null,
            paid_through text,
            note text,
            created_at text not null
        )
        """
    )
    con.commit()
    return con


def sql_cutoff(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def fmt_hours(seconds):
    seconds = int(seconds or 0)
    hours = seconds / 3600
    return f"{hours:,.1f}h"


def fmt_int(value):
    return f"{int(value or 0):,}"


def h(value):
    return html.escape("" if value is None else str(value), quote=True)


def parse_money(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def normalize_user_id(user_id):
    return (user_id or "").replace("-", "").lower()


def load_users():
    users = {}
    with connect_readonly(JELLYFIN_DB) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute(
            """
            select Id, Username, LastLoginDate, LastActivityDate
            from Users
            order by lower(Username)
            """
        ):
            user_id = normalize_user_id(row["Id"])
            users[user_id] = {
                "user_id": user_id,
                "jellyfin_user_id": row["Id"],
                "username": row["Username"],
                "last_login": row["LastLoginDate"],
                "last_activity": row["LastActivityDate"],
                "plays_30": 0,
                "seconds_30": 0,
                "plays_history": 0,
                "seconds_history": 0,
                "total_plays": 0,
                "total_seconds": 0,
                "last_played": None,
                "clients": "",
            }
    return users


def load_playback_summary(users):
    cutoff_30 = sql_cutoff(30)
    cutoff_history = sql_cutoff(HISTORY_DAYS)
    with connect_readonly(PLAYBACK_DB) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute(
            """
            select
                coalesce(nullif(lower(replace(UserId, '-', '')), ''), 'unknown') as user_id,
                count(*) as total_plays,
                coalesce(sum(PlayDuration), 0) as total_seconds,
                sum(case when DateCreated >= ? then 1 else 0 end) as plays_30,
                coalesce(sum(case when DateCreated >= ? then PlayDuration else 0 end), 0) as seconds_30,
                sum(case when DateCreated >= ? then 1 else 0 end) as plays_history,
                coalesce(sum(case when DateCreated >= ? then PlayDuration else 0 end), 0) as seconds_history,
                max(DateCreated) as last_played,
                group_concat(distinct ClientName) as clients
            from PlaybackActivity
            group by user_id
            """,
            (cutoff_30, cutoff_30, cutoff_history, cutoff_history),
        ):
            user_id = row["user_id"]
            user = users.setdefault(
                user_id,
                {
                    "user_id": user_id,
                    "jellyfin_user_id": None,
                    "username": f"Unknown user {user_id[:8]}",
                    "last_login": None,
                    "last_activity": None,
                    "plays_30": 0,
                    "seconds_30": 0,
                    "plays_history": 0,
                    "seconds_history": 0,
                    "total_plays": 0,
                    "total_seconds": 0,
                    "last_played": None,
                    "clients": "",
                },
            )
            user.update(
                {
                    "plays_30": row["plays_30"] or 0,
                    "seconds_30": row["seconds_30"] or 0,
                    "plays_history": row["plays_history"] or 0,
                    "seconds_history": row["seconds_history"] or 0,
                    "total_plays": row["total_plays"] or 0,
                    "total_seconds": row["total_seconds"] or 0,
                    "last_played": row["last_played"],
                    "clients": row["clients"] or "",
                }
            )
    return users


def load_fees():
    with connect_state() as con:
        return {
            row["user_id"]: dict(row)
            for row in con.execute("select * from fee_status")
        }


def payment_status(fee):
    if not fee or not fee.get("paid_through"):
        return "unpaid"
    try:
        paid_through = date.fromisoformat(fee["paid_through"])
    except ValueError:
        return "invalid"
    return "paid" if paid_through >= date.today() else "overdue"


def dashboard():
    users = load_playback_summary(load_users())
    fees = load_fees()
    rows = sorted(users.values(), key=lambda item: item["seconds_30"], reverse=True)
    total_seconds_30 = sum(row["seconds_30"] for row in rows)
    active_30 = sum(1 for row in rows if row["seconds_30"])
    paid_count = sum(1 for row in rows if payment_status(fees.get(row["user_id"])) == "paid")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    body = [
        page_header("Jellyfin usage and fee tracker"),
        "<main>",
        "<section class='cards'>",
        card("Active users, 30d", active_30),
        card("Watch time, 30d", fmt_hours(total_seconds_30)),
        card(f"History window", f"{HISTORY_DAYS}d"),
        card("Paid current fee", paid_count),
        "</section>",
    ]
    if not ADMIN_PASSWORD:
        body.append("<p class='warning'>No dashboard password is set. Keep this reachable only through trusted loopback/Tailscale routes.</p>")
    body.extend(
        [
            f"<p class='meta'>Generated {h(generated_at)}. Playback data comes from <code>playback_reporting.db</code>; fee data is stored separately in <code>{h(STATE_DB)}</code>.</p>",
            "<table>",
            "<thead><tr>",
            "<th>User</th><th class='num'>30d time</th><th class='num'>30d plays</th>",
            f"<th class='num'>{HISTORY_DAYS}d time</th><th>Last played</th><th>Fee status</th><th>Payment tracking</th>",
            "</tr></thead><tbody>",
        ]
    )
    for row in rows:
        fee = fees.get(row["user_id"], {})
        status = payment_status(fee)
        body.append(
            "<tr>"
            f"<td><a href='/user/{quote(row['user_id'])}'>{h(row['username'])}</a><div class='sub'>{h(row['clients'])}</div></td>"
            f"<td class='num strong'>{fmt_hours(row['seconds_30'])}</td>"
            f"<td class='num'>{fmt_int(row['plays_30'])}</td>"
            f"<td class='num'>{fmt_hours(row['seconds_history'])}</td>"
            f"<td>{h(row['last_played'] or row['last_activity'] or '')}</td>"
            f"<td><span class='pill {h(status)}'>{h(status)}</span><div class='sub'>{h(fee.get('paid_through', ''))}</div></td>"
            "<td>"
            f"<form method='post' action='/fee' class='fee-form'>"
            f"<input type='hidden' name='user_id' value='{h(row['user_id'])}'>"
            f"<input type='hidden' name='username' value='{h(row['username'])}'>"
            f"<input name='expected_fee' value='{h(fee.get('expected_fee', DEFAULT_FEE))}' placeholder='fee'>"
            f"<input name='currency' value='{h(fee.get('currency', DEFAULT_CURRENCY))}' placeholder='currency'>"
            f"<input type='date' name='paid_through' value='{h(fee.get('paid_through', ''))}' title='Paid through'>"
            f"<input type='date' name='last_paid_on' value='{h(fee.get('last_paid_on', ''))}' title='Last paid on'>"
            f"<input name='amount' placeholder='amount received'>"
            f"<input name='notes' value='{h(fee.get('notes', ''))}' placeholder='notes'>"
            "<button>Save</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    body.extend(["</tbody></table>", "</main>", page_footer()])
    return "\n".join(body)


def card(label, value):
    return f"<div class='card'><div class='label'>{h(label)}</div><div class='value'>{h(value)}</div></div>"


def user_detail(user_id):
    user_id = normalize_user_id(user_id)
    users = load_playback_summary(load_users())
    user = users.get(user_id, {"username": user_id, "user_id": user_id})
    with connect_readonly(PLAYBACK_DB) as con:
        con.row_factory = sqlite3.Row
        plays = list(
            con.execute(
                """
                select DateCreated, ItemType, ItemName, PlaybackMethod, ClientName, DeviceName, PlayDuration
                from PlaybackActivity
                where lower(replace(UserId, '-', '')) = ? and DateCreated >= ?
                order by DateCreated desc
                limit 250
                """,
                (user_id, sql_cutoff(HISTORY_DAYS)),
            )
        )
    with connect_state() as con:
        payments = list(
            con.execute(
                """
                select amount, currency, paid_on, paid_through, note, created_at
                from payment_events
                where user_id = ?
                order by paid_on desc, id desc
                limit 100
                """,
                (user_id,),
            )
        )
    body = [
        page_header(f"Usage for {user['username']}"),
        "<main>",
        "<p><a href='/'>← Dashboard</a></p>",
        "<section class='cards'>",
        card("30d time", fmt_hours(user.get("seconds_30", 0))),
        card("30d plays", fmt_int(user.get("plays_30", 0))),
        card(f"{HISTORY_DAYS}d time", fmt_hours(user.get("seconds_history", 0))),
        card("Total tracked", fmt_hours(user.get("total_seconds", 0))),
        "</section>",
        "<h2>Recent playback</h2>",
        "<table><thead><tr><th>Date</th><th>Item</th><th>Type</th><th>Client</th><th class='num'>Duration</th></tr></thead><tbody>",
    ]
    for play in plays:
        body.append(
            "<tr>"
            f"<td>{h(play['DateCreated'])}</td>"
            f"<td>{h(play['ItemName'])}<div class='sub'>{h(play['PlaybackMethod'])} · {h(play['DeviceName'])}</div></td>"
            f"<td>{h(play['ItemType'])}</td>"
            f"<td>{h(play['ClientName'])}</td>"
            f"<td class='num'>{fmt_hours(play['PlayDuration'])}</td>"
            "</tr>"
        )
    body.extend(["</tbody></table>", "<h2>Payment events</h2>"])
    if payments:
        body.append("<table><thead><tr><th>Paid on</th><th>Paid through</th><th class='num'>Amount</th><th>Note</th></tr></thead><tbody>")
        for payment in payments:
            body.append(
                "<tr>"
                f"<td>{h(payment['paid_on'])}</td>"
                f"<td>{h(payment['paid_through'])}</td>"
                f"<td class='num'>{h(payment['amount'])} {h(payment['currency'])}</td>"
                f"<td>{h(payment['note'])}</td>"
                "</tr>"
            )
        body.append("</tbody></table>")
    else:
        body.append("<p class='meta'>No payment events recorded for this user.</p>")
    body.extend(["</main>", page_footer()])
    return "\n".join(body)


def save_fee(fields):
    user_id = normalize_user_id(fields.get("user_id", [""])[0])
    username = fields.get("username", [""])[0] or user_id
    expected_fee = parse_money(fields.get("expected_fee", [""])[0])
    currency = fields.get("currency", [DEFAULT_CURRENCY])[0] or DEFAULT_CURRENCY
    paid_through = fields.get("paid_through", [""])[0]
    last_paid_on = fields.get("last_paid_on", [""])[0]
    amount = parse_money(fields.get("amount", [""])[0])
    notes = fields.get("notes", [""])[0]
    now = datetime.now().isoformat(timespec="seconds")
    with connect_state() as con:
        con.execute(
            """
            insert into fee_status (user_id, username, expected_fee, currency, paid_through, last_paid_on, notes, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(user_id) do update set
                username = excluded.username,
                expected_fee = excluded.expected_fee,
                currency = excluded.currency,
                paid_through = excluded.paid_through,
                last_paid_on = excluded.last_paid_on,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (user_id, username, expected_fee, currency, paid_through, last_paid_on, notes, now),
        )
        if amount is not None:
            con.execute(
                """
                insert into payment_events (user_id, username, amount, currency, paid_on, paid_through, note, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, amount, currency, last_paid_on or date.today().isoformat(), paid_through, notes, now),
            )
        con.commit()


def page_header(title):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)}</title>
<style>
:root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #e2e8f0; }}
body {{ margin: 0; }}
header {{ padding: 28px 32px; background: linear-gradient(135deg, #111827, #1e293b); border-bottom: 1px solid #334155; }}
main {{ padding: 24px 32px 48px; }}
h1 {{ margin: 0; font-size: 28px; }}
h2 {{ margin-top: 32px; }}
a {{ color: #93c5fd; }}
code {{ color: #bfdbfe; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 18px; background: #111827; border: 1px solid #334155; border-radius: 12px; overflow: hidden; }}
th, td {{ padding: 10px 12px; border-bottom: 1px solid #1f2937; vertical-align: top; }}
th {{ text-align: left; background: #1e293b; color: #cbd5e1; font-size: 13px; }}
tr:last-child td {{ border-bottom: 0; }}
input {{ width: 9.5rem; padding: 7px 8px; border: 1px solid #475569; border-radius: 8px; background: #0f172a; color: #e2e8f0; }}
button {{ padding: 7px 10px; border: 0; border-radius: 8px; background: #2563eb; color: white; cursor: pointer; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 18px; }}
.card {{ background: #111827; border: 1px solid #334155; border-radius: 14px; padding: 16px; }}
.label {{ color: #94a3b8; font-size: 13px; }}
.value {{ margin-top: 6px; font-size: 24px; font-weight: 700; }}
.num {{ text-align: right; white-space: nowrap; }}
.strong {{ font-weight: 700; }}
.sub, .meta {{ color: #94a3b8; font-size: 12px; }}
.warning {{ padding: 12px 14px; border: 1px solid #f59e0b; border-radius: 10px; background: #451a03; color: #fde68a; }}
.pill {{ display: inline-block; min-width: 4.5rem; text-align: center; padding: 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
.paid {{ background: #064e3b; color: #a7f3d0; }}
.overdue, .unpaid, .invalid {{ background: #7f1d1d; color: #fecaca; }}
.fee-form {{ display: flex; flex-wrap: wrap; gap: 6px; }}
</style>
</head>
<body>
<header><h1>{h(title)}</h1></header>"""


def page_footer():
    return "</body></html>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def authorized(self):
        if not ADMIN_PASSWORD or self.path == "/health":
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header.removeprefix("Basic "), validate=True).decode()
        except Exception:
            return False
        _, _, password = decoded.partition(":")
        return password == ADMIN_PASSWORD

    def require_auth(self):
        if self.authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Jellyfin Usage"')
        self.end_headers()
        self.wfile.write(b"Authentication required")
        return False

    def send_html(self, content, status=200):
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        if not self.authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Jellyfin Usage"')
            self.end_headers()
            return
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(204)
        elif parsed.path == "/" or parsed.path.startswith("/user/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(204)
            self.end_headers()
        elif parsed.path == "/":
            self.send_html(dashboard())
        elif parsed.path.startswith("/user/"):
            self.send_html(user_detail(unquote(parsed.path.removeprefix("/user/"))))
        else:
            self.send_html(page_header("Not found") + "<main><p>Not found.</p></main>" + page_footer(), 404)

    def do_POST(self):
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path != "/fee":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        save_fee(fields)
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


if __name__ == "__main__":
    connect_state().close()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Serving Jellyfin usage tracker on {HOST}:{PORT}", flush=True)
    server.serve_forever()
