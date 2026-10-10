import os
import http.server
import socketserver
import sqlite3
import hashlib
import secrets
import html
import re
import json
import time
from urllib.parse import urlparse, parse_qs

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8080))
DB_FILE = "nova_chat.db"
ONLINE_SECONDS = 45

sessions = {}


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def add_column(conn, table, column, definition):
    if column not in table_columns(conn, table):
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def setup_database():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT 0,
            last_seen INTEGER DEFAULT 0
        )
    """)

    # خودکار هماهنگ کردن دیتابیس‌های قدیمی
    add_column(conn, "users", "created_at",
               "INTEGER NOT NULL DEFAULT 0")
    add_column(conn, "users", "last_seen",
               "INTEGER DEFAULT 0")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            receiver_id INTEGER,
            message TEXT,
            created_at INTEGER NOT NULL DEFAULT 0
        )
    """)

    # ستون‌های نسخه‌های قبلی
    add_column(conn, "messages", "sender_id", "INTEGER")
    add_column(conn, "messages", "receiver_id", "INTEGER")
    add_column(conn, "messages", "message", "TEXT")
    add_column(conn, "messages", "created_at",
               "INTEGER NOT NULL DEFAULT 0")
    add_column(conn, "messages", "seen",
               "INTEGER DEFAULT 0")

    # اگر دیتابیس قدیمی ستون sender/receiver داشته باشد
    # اطلاعات آن‌ها را به ستون‌های جدید منتقل می‌کنیم.
    cols = table_columns(conn, "messages")

    if "sender" in cols:
        conn.execute("""
            UPDATE messages
            SET sender_id = sender
            WHERE sender_id IS NULL
        """)

    if "receiver" in cols:
        conn.execute("""
            UPDATE messages
            SET receiver_id = receiver
            WHERE receiver_id IS NULL
        """)

    if "text" in cols:
        conn.execute("""
            UPDATE messages
            SET message = text
            WHERE message IS NULL
        """)

    conn.execute("""
        UPDATE messages
        SET seen = 0
        WHERE seen IS NULL
    """)

    now = int(time.time())

    conn.execute("""
        UPDATE users
        SET created_at = ?
        WHERE created_at IS NULL OR created_at = 0
    """, (now,))

    conn.execute("""
        UPDATE messages
        SET created_at = ?
        WHERE created_at IS NULL OR created_at = 0
    """, (now,))

    conn.commit()
    conn.close()


setup_database()


# =========================================================
# HELPERS
# =========================================================

def now():
    return int(time.time())


def hash_password(password):
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def valid_username(username):
    if not username:
        return False
    if not re.fullmatch(r"[A-Za-z0-9]+", username):
        return False
    if not re.search(r"[0-9]", username):
        return False
    return True


def valid_password(password):
    if not password or len(password) < 4:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9]+", password))


def clean_text(text, limit=3000):
    text = str(text or "").strip()
    return text[:limit]


def get_user_by_id(user_id):
    conn = db()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    return user


def get_user_by_username(username):
    conn = db()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ?",
        (username,)
    ).fetchone()
    conn.close()
    return user


def update_last_seen(user_id):
    conn = db()
    conn.execute(
        "UPDATE users SET last_seen = ? WHERE id = ?",
        (now(), user_id)
    )
    conn.commit()
    conn.close()


def is_online(user):
    if not user:
        return False

    last_seen = user["last_seen"] or 0
    return now() - int(last_seen) <= ONLINE_SECONDS


def split_cookie(value):
    result = {}

    for part in value.split(";"):
        if "=" in part:
            key, val = part.strip().split("=", 1)
            result[key] = val

    return result


def current_user(handler):
    cookies = split_cookie(
        handler.headers.get("Cookie", "")
    )

    sid = cookies.get("session")

    if not sid:
        return None

    user_id = sessions.get(sid)

    if not user_id:
        return None

    user = get_user_by_id(user_id)

    if user:
        update_last_seen(user_id)

    return user


def make_session(user_id):
    sid = secrets.token_urlsafe(32)
    sessions[sid] = user_id
    return sid


def parse_post(handler):
    length = int(
        handler.headers.get("Content-Length", "0")
    )

    raw = handler.rfile.read(length).decode(
        "utf-8",
        errors="replace"
    )

    return parse_qs(raw)


def post_value(data, key, default=""):
    value = data.get(key, [default])
    return value[0] if value else default


# =========================================================
# HTML
# =========================================================

CSS = """
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #080b14;
    color: #f4f7ff;
    font-family: Arial, sans-serif;
}

a {
    color: inherit;
    text-decoration: none;
}

.container {
    width: min(100% - 24px, 760px);
    margin: 25px auto;
}

.topbar {
    background: #101625;
    border: 1px solid #202a40;
    border-radius: 18px;
    padding: 14px;
    margin-bottom: 14px;
}

.brand {
    font-size: 23px;
    font-weight: bold;
    letter-spacing: 1px;
}

.nav {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 12px;
}

button,
.btn {
    border: 0;
    border-radius: 12px;
    background: #5865f2;
    color: white;
    padding: 11px 15px;
    cursor: pointer;
    font-size: 14px;
}

.btn.dark,
button.dark {
    background: #20283a;
}

.btn.danger,
button.danger {
    background: #a92e46;
}

.card {
    background: #101625;
    border: 1px solid #202a40;
    border-radius: 18px;
    padding: 18px;
    margin-bottom: 14px;
}

.section-title {
    font-size: 20px;
    font-weight: bold;
    margin-bottom: 14px;
}

input,
textarea {
    width: 100%;
    border: 1px solid #2a3550;
    background: #080d18;
    color: white;
    border-radius: 12px;
    padding: 12px;
    outline: none;
    margin-bottom: 10px;
}

textarea {
    resize: vertical;
    min-height: 90px;
}

.small {
    color: #98a4bc;
    font-size: 13px;
}

.error {
    background: #3a1720;
    border: 1px solid #743244;
    padding: 12px;
    border-radius: 12px;
    margin-bottom: 12px;
}

.success {
    background: #123326;
    border: 1px solid #245d45;
    padding: 12px;
    border-radius: 12px;
    margin-bottom: 12px;
}

.user-row,
.chat-row {
    background: #0c1220;
    border: 1px solid #202a40;
    border-radius: 15px;
    padding: 14px;
    margin: 9px 0;
}

.user-row:hover,
.chat-row:hover {
    border-color: #5865f2;
}

.username {
    font-weight: bold;
    font-size: 17px;
}

.online {
    color: #4ade80;
}

.offline {
    color: #8b95a8;
}

.messages {
    display: flex;
    flex-direction: column;
    gap: 9px;
    max-height: 58vh;
    overflow-y: auto;
    padding: 5px;
}

.message {
    max-width: 82%;
    padding: 11px 13px;
    border-radius: 15px;
    background: #182135;
    align-self: flex-start;
    word-wrap: break-word;
}

.message.mine {
    background: #3844a8;
    align-self: flex-end;
}

.message-meta {
    color: #aeb8cc;
    font-size: 11px;
    margin-top: 6px;
}

.message-actions {
    margin-top: 7px;
}

.delete-btn {
    background: transparent;
    color: #ff9aaa;
    border: 1px solid #6e2c3b;
    padding: 5px 8px;
    border-radius: 8px;
    font-size: 11px;
}

.chat-head {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    align-items: center;
}

.chat-form {
    display: flex;
    gap: 8px;
    margin-top: 12px;
}

.chat-form input {
    margin: 0;
    flex: 1;
}


.send-btn {
    width: 54px;
    min-width: 54px;
    padding: 10px 0;
    font-size: 22px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
}

.ticks {
    margin-right: 5px;
    letter-spacing: -2px;
    font-size: 13px;
}

.center {
    text-align: center;
}

.auth-wrap {
    min-height: 72vh;
    display: flex;
    align-items: center;
    justify-content: center;
}

.auth-card {
    width: min(100%, 430px);
}

@media (max-width: 600px) {
    .container {
        width: min(100% - 14px, 760px);
        margin: 10px auto;
    }

    .card {
        padding: 13px;
    }

    .message {
        max-width: 90%;
    }

    .chat-form {
        flex-direction: column;
    }

    .chat-form button:not(.send-btn) {
        width: 100%;
    }

    .chat-form .send-btn {
        width: 54px;
        min-width: 54px;
    }
}
"""


def page(title, body, user=None):
    nav = ""

    if user:
        nav = f"""
        <div class="nav">
            <a class="btn dark" href="/">🏠 خانه</a>
            <a class="btn dark" href="/conversations">💬 پیام‌رسان</a>
            <a class="btn dark" href="/profile">👤 پروفایل</a>
            <form method="post" action="/logout"
                  style="display:inline;">
                <button class="danger">🚪 خروج</button>
            </form>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - NOVA CHAT</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

<div class="topbar">
    <div class="brand">✦ NOVA CHAT</div>
    {nav}
</div>

{body}

</div>
</body>
</html>
"""


# =========================================================
# PAGES
# =========================================================

def login_page(error=""):
    err = (
        f'<div class="error">{html.escape(error)}</div>'
        if error else ""
    )

    body = f"""
    <div class="auth-wrap">
    <div class="card auth-card">
        <div class="section-title">ورود به NOVA CHAT</div>
        {err}

        <form method="post" action="/login">
            <input
                name="username"
                placeholder="نام کاربری"
                autocomplete="username"
                required
            >

            <input
                name="password"
                type="password"
                placeholder="رمز عبور"
                autocomplete="current-password"
                required
            >

            <button>ورود</button>
        </form>

        <br>

        <a class="btn dark" href="/register">
            ساخت حساب
        </a>
    </div>
    </div>
    """

    return page("ورود", body)


def register_page(error=""):
    err = (
        f'<div class="error">{html.escape(error)}</div>'
        if error else ""
    )

    body = f"""
    <div class="auth-wrap">
    <div class="card auth-card">
        <div class="section-title">ساخت حساب</div>

        {err}

        <div class="small">
            نام کاربری فقط انگلیسی و عدد باشد و حداقل یک عدد داشته باشد.
            رمز فقط انگلیسی و عدد و حداقل ۴ کاراکتر.
        </div>

        <br>

        <form method="post" action="/register">

            <input
                name="username"
                placeholder="مثلاً AMIRAli1391"
                required
            >

            <input
                name="password"
                type="password"
                placeholder="رمز عبور"
                required
            >

            <input
                name="password_confirm"
                type="password"
                placeholder="تکرار رمز عبور"
                required
            >

            <button>ثبت‌نام</button>
        </form>

        <br>

        <a class="btn dark" href="/login">
            قبلاً حساب دارم
        </a>
    </div>
    </div>
    """

    return page("ثبت‌نام", body)


def home_page(user, query=""):
    results_html = ""

    if query:
        conn = db()

        # جستجوی دقیق:
        # فقط وقتی کل نام کاربری وارد شده باشد نتیجه می‌آید.
        found = conn.execute(
            """
            SELECT *
            FROM users
            WHERE username = ?
              AND id != ?
            LIMIT 1
            """,
            (query, user["id"])
        ).fetchone()

        conn.close()

        if found:
            status = (
                '<span class="online">● آنلاین</span>'
                if is_online(found)
                else '<span class="offline">● آفلاین</span>'
            )

            results_html = f"""
            <div class="user-row">
                <div class="username">
                    {html.escape(found["username"])}
                </div>

                <div class="small">
                    {status}
                </div>

                <br>

                <a class="btn"
                   href="/chat?id={found["id"]}">
                    💬 پیام
                </a>
            </div>
            """

        else:
            results_html = """
            <div class="card">
                <div class="small">
                    کاربری با این نام کاربری پیدا نشد.
                </div>
            </div>
            """

    body = f"""
    <div class="card">
        <div class="section-title">
            🔎 پیدا کردن کاربر
        </div>

        <form method="get" action="/">
            <input
                name="q"
                value="{html.escape(query)}"
                placeholder="نام کاربری کامل را وارد کن"
                autocomplete="off"
            >
            <button>جستجو</button>
        </form>

        <div class="small">
            برای پیدا شدن کاربر باید نام کاربری را کامل وارد کنی.
        </div>
    </div>

    {results_html}
    """

    return page("خانه", body, user)


def conversations_page(user):
    conn = db()

    rows = conn.execute(
        """
        SELECT
            u.id,
            u.username,
            u.last_seen,
            MAX(m.created_at) AS last_message
        FROM messages m
        JOIN users u
          ON u.id =
             CASE
                 WHEN m.sender_id = ? THEN m.receiver_id
                 ELSE m.sender_id
             END
        WHERE m.sender_id = ?
           OR m.receiver_id = ?
        GROUP BY u.id
        ORDER BY last_message DESC
        """,
        (user["id"], user["id"], user["id"])
    ).fetchall()

    conn.close()

    chats = ""

    for row in rows:
        status = (
            '<span class="online">● آنلاین</span>'
            if is_online(row)
            else '<span class="offline">● آفلاین</span>'
        )

        chats += f"""
        <a href="/chat?id={row["id"]}">
            <div class="chat-row">
                <div class="username">
                    {html.escape(row["username"])}
                </div>

                <div class="small">
                    {status}
                </div>
            </div>
        </a>
        """

    if not chats:
        chats = """
        <div class="small">
            هنوز با کسی گفت‌وگویی نداری.
            از خانه یک نام کاربری کامل را جستجو کن.
        </div>
        """

    body = f"""
    <div class="card">
        <div class="section-title">
            💬 پیام‌رسان
        </div>

        <div class="small">
            گفتگوهای شما اینجا نمایش داده می‌شوند.
        </div>
    </div>

    <div class="card">
        {chats}
    </div>
    """

    return page("پیام‌رسان", body, user)


def profile_page(user, error="", success=""):
    err = (
        f'<div class="error">{html.escape(error)}</div>'
        if error else ""
    )

    ok = (
        f'<div class="success">{html.escape(success)}</div>'
        if success else ""
    )

    status = (
        '<span class="online">● آنلاین</span>'
        if is_online(user)
        else '<span class="offline">● آفلاین</span>'
    )

    joined = time.strftime(
        "%Y/%m/%d",
        time.localtime(user["created_at"] or now())
    )

    body = f"""
    <div class="card">
        <div class="section-title">
            👤 پروفایل
        </div>

        {err}
        {ok}

        <div class="username">
            {html.escape(user["username"])}
        </div>

        <p>{status}</p>

        <div class="small">
            تاریخ عضویت: {joined}
        </div>
    </div>

    <div class="card">
        <div class="section-title">
            تغییر نام کاربری
        </div>

        <form method="post" action="/change_username">
            <input
                name="username"
                placeholder="نام کاربری جدید"
                required
            >
            <button>ذخیره</button>
        </form>
    </div>
    """

    return page("پروفایل", body, user)


def chat_page(user, other_id):
    other = get_user_by_id(other_id)

    # وقتی گیرنده وارد گفتگو می‌شود، پیام‌های دریافتی دیده‌شده می‌شوند.
    with db() as conn:
        conn.execute("""
            UPDATE messages
            SET seen = 1
            WHERE receiver_id = ? AND sender_id = ?
        """, (user["id"], other_id))
        conn.commit()

    if not other:
        return page(
            "خطا",
            """
            <div class="card">
                کاربر پیدا نشد.
            </div>
            """,
            user
        )

    conn = db()

    messages = conn.execute(
        """
        SELECT *
        FROM messages
        WHERE
            (sender_id = ? AND receiver_id = ?)
            OR
            (sender_id = ? AND receiver_id = ?)
        ORDER BY id ASC
        """,
        (
            user["id"],
            other["id"],
            other["id"],
            user["id"]
        )
    ).fetchall()

    conn.close()

    message_html = ""

    for msg in messages:
        mine = msg["sender_id"] == user["id"]

        cls = "message mine" if mine else "message"

        timestamp = time.strftime(
            "%H:%M",
            time.localtime(msg["created_at"] or now())
        )

        seen_mark = "✓✓" if mine and msg["seen"] else ("✓" if mine else "")

        delete_html = ""

        if mine:
            delete_html = f"""
            <form method="post"
                  action="/delete_message"
                  class="message-actions">
                <input type="hidden"
                       name="message_id"
                       value="{msg["id"]}">
                <input type="hidden"
                       name="receiver_id"
                       value="{other["id"]}">
                <button class="delete-btn">
                    🗑 حذف
                </button>
            </form>
            """

        message_html += f"""
        <div class="{cls}">
            <div>
                {html.escape(msg["message"] or "")}
            </div>

            <div class="message-meta">
                {timestamp} <span class="ticks">{seen_mark}</span>
            </div>

            {delete_html}
        </div>
        """

    status = (
        '<span id="chat-status" class="online">● آنلاین</span>'
        if is_online(other)
        else '<span id="chat-status" class="offline">● آفلاین</span>'
    )

    body = f"""
    <div class="card">

        <div class="chat-head">

            <div>
                <div class="username">
                    {html.escape(other["username"])}
                </div>

                <div>
                    {status}
                </div>
            </div>

            <a class="btn dark"
               href="/conversations">
                💬 پیام‌رسان
            </a>

        </div>

    </div>

    <div class="card">

        <div class="messages" id="messages">
            {message_html}
        </div>

        <form method="post"
              action="/send"
              class="chat-form">

            <input
                name="message"
                placeholder="پیامت رو بنویس..."
                autocomplete="off"
                required
            >

            <input
                type="hidden"
                name="receiver_id"
                value="{other["id"]}"
            >

            <button class="send-btn" type="submit" aria-label="ارسال" title="ارسال">🚀</button>

        </form>

    </div>

<script>
setInterval(function() {{
    fetch("/heartbeat", {{
        method: "POST"
    }}).catch(function(){{}});
}}, 15000);

setInterval(function() {{
    fetch("/status?id={other["id"]}")
        .then(function(r) {{
            return r.json();
        }})
        .then(function(data) {{
            var el = document.getElementById("chat-status");

            if (!el) return;

            if (data.online) {{
                el.className = "online";
                el.textContent = "● آنلاین";
            }} else {{
                el.className = "offline";
                el.textContent = "● آفلاین";
            }}
        }})
        .catch(function(){{}});
}}, 15000);
</script>
    """

    return page(
        "گفتگو با " + other["username"],
        body,
        user
    )


# =========================================================
# HTTP HANDLER
# =========================================================

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(
            "%s - - [%s] %s"
            % (
                self.address_string(),
                self.log_date_time_string(),
                format % args
            )
        )

    def send_html(self, content, status=200):
        data = content.encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )
        self.send_header(
            "Content-Length",
            str(len(data))
        )
        self.end_headers()

        self.wfile.write(data)

    def send_json(self, data, status=200):
        raw = json.dumps(
            data,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header(
            "Content-Length",
            str(len(raw))
        )
        self.end_headers()

        self.wfile.write(raw)

    def redirect(self, location):
        self.send_response(303)
        self.send_header(
            "Location",
            location
        )
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # favicon
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        user = current_user(self)

        # -------------------------------------------------
        # LOGIN
        # -------------------------------------------------

        if path == "/login":

            if user:
                self.redirect("/")
                return

            self.send_html(login_page())
            return

        # -------------------------------------------------
        # REGISTER
        # -------------------------------------------------

        if path == "/register":

            if user:
                self.redirect("/")
                return

            self.send_html(register_page())
            return

        # -------------------------------------------------
        # HOME
        # -------------------------------------------------

        if path == "/":

            if not user:
                self.redirect("/login")
                return

            q = query.get("q", [""])[0]

            self.send_html(
                home_page(user, q)
            )
            return

        # -------------------------------------------------
        # CONVERSATIONS
        # -------------------------------------------------

        if path == "/conversations":

            if not user:
                self.redirect("/login")
                return

            self.send_html(
                conversations_page(user)
            )
            return

        # -------------------------------------------------
        # PROFILE
        # -------------------------------------------------

        if path == "/profile":

            if not user:
                self.redirect("/login")
                return

            self.send_html(
                profile_page(user)
            )
            return

        # -------------------------------------------------
        # CHAT
        # -------------------------------------------------

        if path == "/chat":

            if not user:
                self.redirect("/login")
                return

            try:
                other_id = int(
                    query.get("id", ["0"])[0]
                )
            except ValueError:
                self.send_html(
                    page(
                        "خطا",
                        '<div class="card">شناسه کاربر نامعتبر است.</div>',
                        user
                    ),
                    400
                )
                return

            if other_id == user["id"]:
                self.redirect("/")
                return

            self.send_html(
                chat_page(user, other_id)
            )
            return

        # -------------------------------------------------
        # STATUS
        # -------------------------------------------------

        if path == "/status":

            if not user:
                self.send_json(
                    {"online": False},
                    401
                )
                return

            try:
                other_id = int(
                    query.get("id", ["0"])[0]
                )
            except ValueError:
                self.send_json(
                    {"online": False},
                    400
                )
                return

            other = get_user_by_id(other_id)

            self.send_json({
                "online": bool(
                    other and is_online(other)
                )
            })
            return

        # -------------------------------------------------
        # UNKNOWN
        # -------------------------------------------------

        self.send_html(
            page(
                "خطا",
                """
                <div class="card">

                    <div class="section-title">
                        درخواست نامعتبر است.
                    </div>

                    <a href="/">
                        <button>
                            برگشت
                        </button>
                    </a>

                </div>
                """
            ),
            404
        )

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        data = parse_post(self)

        user = current_user(self)

        # -------------------------------------------------
        # REGISTER
        # -------------------------------------------------

        if path == "/register":

            if user:
                self.redirect("/")
                return

            username = clean_text(
                post_value(data, "username"),
                50
            )

            password = post_value(
                data,
                "password"
            )

            password_confirm = post_value(
                data,
                "password_confirm"
            )

            if not valid_username(username):
                self.send_html(
                    register_page(
                        "نام کاربری باید فقط انگلیسی و عدد باشد "
                        "و حداقل یک عدد داشته باشد."
                    ),
                    400
                )
                return

            if not valid_password(password):
                self.send_html(
                    register_page(
                        "رمز باید حداقل ۴ کاراکتر و فقط شامل "
                        "حروف انگلیسی و عدد باشد."
                    ),
                    400
                )
                return

            if password != password_confirm:
                self.send_html(
                    register_page(
                        "تکرار رمز عبور با رمز عبور یکسان نیست."
                    ),
                    400
                )
                return

            conn = db()

            try:
                conn.execute(
                    """
                    INSERT INTO users
                    (username, password, created_at, last_seen)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        username,
                        hash_password(password),
                        now(),
                        now()
                    )
                )

                conn.commit()

            except sqlite3.IntegrityError:
                conn.close()

                self.send_html(
                    register_page(
                        "این نام کاربری قبلاً استفاده شده."
                    ),
                    400
                )
                return

            conn.close()

            self.redirect("/login")
            return

        # -------------------------------------------------
        # LOGIN
        # -------------------------------------------------

        if path == "/login":

            if user:
                self.redirect("/")
                return

            username = clean_text(
                post_value(data, "username"),
                50
            )

            password = post_value(
                data,
                "password"
            )

            found = get_user_by_username(username)

            if (
                not found
                or found["password"]
                != hash_password(password)
            ):
                self.send_html(
                    login_page(
                        "نام کاربری یا رمز عبور اشتباه است."
                    ),
                    401
                )
                return

            sid = make_session(found["id"])

            self.send_response(303)
            self.send_header(
                "Location",
                "/"
            )
            self.send_header(
                "Set-Cookie",
                f"session={sid}; Path=/; HttpOnly; SameSite=Lax"
            )
            self.end_headers()

            update_last_seen(found["id"])
            return

        # -------------------------------------------------
        # LOGOUT
        # -------------------------------------------------

        if path == "/logout":

            if not user:
                self.redirect("/login")
                return

            cookies = split_cookie(
                self.headers.get("Cookie", "")
            )

            sid = cookies.get("session")

            if sid:
                sessions.pop(sid, None)

            conn = db()

            conn.execute(
                "UPDATE users SET last_seen = 0 WHERE id = ?",
                (user["id"],)
            )

            conn.commit()
            conn.close()

            self.send_response(303)
            self.send_header(
                "Location",
                "/login"
            )
            self.send_header(
                "Set-Cookie",
                "session=; Path=/; Max-Age=0; HttpOnly"
            )
            self.end_headers()
            return

        # -------------------------------------------------
        # HEARTBEAT
        # -------------------------------------------------

        if path == "/heartbeat":

            if not user:
                self.send_json(
                    {"ok": False},
                    401
                )
                return

            update_last_seen(user["id"])

            self.send_json(
                {"ok": True}
            )
            return

        # -------------------------------------------------
        # SEND MESSAGE
        # -------------------------------------------------

        if path == "/send":

            if not user:
                self.redirect("/login")
                return

            try:
                receiver_id = int(
                    post_value(data, "receiver_id", "0")
                )
            except ValueError:
                self.send_html(
                    page(
                        "خطا",
                        '<div class="card">کاربر نامعتبر است.</div>',
                        user
                    ),
                    400
                )
                return

            message = clean_text(
                post_value(data, "message"),
                3000
            )

            receiver = get_user_by_id(receiver_id)

            if not receiver:
                self.send_html(
                    page(
                        "خطا",
                        '<div class="card">کاربر پیدا نشد.</div>',
                        user
                    ),
                    404
                )
                return

            if not message:
                self.redirect(
                    "/chat?id=" + str(receiver_id)
                )
                return

            conn = db()

            # سازگار با ساختار جدید دیتابیس
            conn.execute(
                """
                INSERT INTO messages
                (sender_id, receiver_id, message, created_at, seen)
                VALUES (?, ?, ?, ?, 0)
                """,
                (
                    user["id"],
                    receiver_id,
                    message,
                    now()
                )
            )

            conn.commit()
            conn.close()

            update_last_seen(user["id"])

            self.redirect(
                "/chat?id=" + str(receiver_id)
            )
            return

        # -------------------------------------------------
        # DELETE MESSAGE
        # -------------------------------------------------

        if path == "/delete_message":

            if not user:
                self.redirect("/login")
                return

            try:
                message_id = int(
                    post_value(data, "message_id", "0")
                )
            except ValueError:
                message_id = 0

            try:
                receiver_id = int(
                    post_value(data, "receiver_id", "0")
                )
            except ValueError:
                receiver_id = 0

            conn = db()

            # فقط فرستنده خودش می‌تواند پیام خودش را حذف کند
            conn.execute(
                """
                DELETE FROM messages
                WHERE id = ?
                  AND sender_id = ?
                """,
                (
                    message_id,
                    user["id"]
                )
            )

            conn.commit()
            conn.close()

            self.redirect(
                "/chat?id=" + str(receiver_id)
            )
            return

        # -------------------------------------------------
        # CHANGE USERNAME
        # -------------------------------------------------

        if path == "/change_username":

            if not user:
                self.redirect("/login")
                return

            username = clean_text(
                post_value(data, "username"),
                50
            )

            if not valid_username(username):
                self.send_html(
                    profile_page(
                        user,
                        error=(
                            "نام کاربری باید فقط انگلیسی و عدد باشد "
                            "و حداقل یک عدد داشته باشد."
                        )
                    ),
                    400
                )
                return

            if username == user["username"]:
                self.send_html(
                    profile_page(
                        user,
                        error="این همان نام کاربری فعلی است."
                    ),
                    400
                )
                return

            conn = db()

            try:
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?
                    WHERE id = ?
                    """,
                    (
                        username,
                        user["id"]
                    )
                )

                conn.commit()

            except sqlite3.IntegrityError:
                conn.close()

                self.send_html(
                    profile_page(
                        user,
                        error="این نام کاربری قبلاً گرفته شده."
                    ),
                    400
                )
                return

            conn.close()

            new_user = get_user_by_id(user["id"])

            self.send_html(
                profile_page(
                    new_user,
                    success="نام کاربری با موفقیت تغییر کرد."
                )
            )
            return

        # -------------------------------------------------
        # UNKNOWN
        # -------------------------------------------------

        self.send_html(
            page(
                "خطا",
                """
                <div class="card">

                    <div class="section-title">
                        درخواست نامعتبر است.
                    </div>

                    <a href="/">
                        <button>
                            برگشت
                        </button>
                    </a>

                </div>
                """
            ),
            400
        )


# =========================================================
# SERVER
# =========================================================

class ThreadedServer(
    socketserver.ThreadingMixIn,
    http.server.HTTPServer
):
    daemon_threads = True
    allow_reuse_address = True


def main():

    print()
    print("=" * 55)
    print("              NOVA CHAT")
    print("=" * 55)
    print()
    print("Server started:")
    print(f"http://{HOST}:{PORT}")
    print()
    print("برای توقف سرور: Ctrl + C")
    print("=" * 55)

    server = ThreadedServer(
        (HOST, PORT),
        Handler
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print()
        print("Server stopped.")

    finally:
        server.server_close()


if __name__ == "__main__":
    main()
