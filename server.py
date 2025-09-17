from flask import Flask, request, render_template, redirect, url_for, flash, session, jsonify
from flask_socketio import SocketIO, emit, join_room
from datetime import datetime
from threading import Lock
import sqlite3
import os
import random

# ----------------------------
# Flask + SocketIO setup
# ----------------------------
app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "supersecretkey")

# Use eventlet for production-safe async
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")
lock = Lock()
clients = {}  # username -> True (online map)

DB_FILE = "chat.db"

# ----------------------------
# Database helpers
# ----------------------------
def _connect():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = _connect()
    c = conn.cursor()

    # users table
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    c.execute("PRAGMA table_info(users)")
    cols = {row[1] for row in c.fetchall()}
    if "private_key" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN private_key INTEGER")
    if "public_key" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN public_key INTEGER")

    # messages table
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT,
            message TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def register_user(username, password, private_key, public_key):
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username, password, private_key, public_key) VALUES (?, ?, ?, ?)",
                  (username, password, private_key, public_key))
        conn.commit()
        return c.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()

def get_user(username):
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT id, username, password, private_key, public_key FROM users WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()
    return row

def get_all_users():
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT id, username, private_key, public_key FROM users ORDER BY username ASC")
    rows = c.fetchall()
    conn.close()
    return rows

def save_message(sender, recipient, message, ts):
    conn = _connect()
    c = conn.cursor()
    c.execute("INSERT INTO messages (sender, recipient, message, timestamp) VALUES (?, ?, ?, ?)",
              (sender, recipient, message, ts))
    msg_id = c.lastrowid
    conn.commit()
    conn.close()
    return msg_id

def get_all_messages():
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT id, sender, recipient, message, timestamp FROM messages ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_group_messages():
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT id, sender, recipient, message, timestamp FROM messages WHERE recipient IS NULL ORDER BY id ASC")
    msgs = c.fetchall()
    conn.close()
    return msgs

def get_private_messages(user1, user2):
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        SELECT id, sender, recipient, message, timestamp
        FROM messages
        WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?)
        ORDER BY id ASC
    """, (user1, user2, user2, user1))
    msgs = c.fetchall()
    conn.close()
    return msgs

def delete_message_by_id(msg_id, username):
    conn = _connect()
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE id=? AND sender=?", (msg_id, username))
    conn.commit()
    conn.close()

def delete_group_messages():
    conn = _connect()
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE recipient IS NULL")
    conn.commit()
    conn.close()

# -------------------------------
# Key exchange (demo parameters)
# -------------------------------
DEMO_PARAMS = {"p": 47, "c": 3, "SEED": 5}

# -------------------------------
# Routes
# -------------------------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        if not username or not password:
            flash("Both fields required.", "error")
            return redirect(url_for("login"))

        with lock:
            user = get_user(username)
            if user:
                user_id, db_username, db_password, priv, pub = user
                if db_password == password:
                    session["username"] = username
                    flash("Login successful!", "success")
                    return redirect(url_for("chat"))
                else:
                    flash("Invalid password.", "error")
                    return redirect(url_for("login"))
            else:
                priv = random.randint(2, 15)
                p, c, seed = DEMO_PARAMS["p"], DEMO_PARAMS["c"], DEMO_PARAMS["SEED"]
                pub = (pow(c, priv, p) * seed) % p
                user_id = register_user(username, password, priv, pub)
                if user_id:
                    session["username"] = username
                    flash("Registered successfully!", "success")
                    return redirect(url_for("chat"))
                else:
                    flash("Username already exists.", "error")
                    return redirect(url_for("login"))
    return render_template("login.html")

@app.route("/chat")
def chat():
    if "username" not in session:
        flash("Please log in.", "error")
        return redirect(url_for("login"))
    return render_template("chat.html", username=session["username"])

@app.route("/logout")
def logout():
    if "username" in session:
        uname = session["username"]
        with lock:
            clients.pop(uname, None)
        session.pop("username")
        socketio.emit("user_status", {"users": list(clients.keys())})
    return redirect(url_for("login"))

@app.route("/history/group")
def history_group():
    if "username" not in session:
        return jsonify({"error": "Not logged in"}), 403
    msgs = get_group_messages()
    return jsonify([{"id": m[0], "sender": m[1], "recipient": m[2], "message": m[3], "timestamp": m[4]} for m in msgs])

@app.route("/history/<other_user>")
def history_private(other_user):
    if "username" not in session:
        return jsonify({"error": "Not logged in"}), 403
    me = session["username"]
    msgs = get_private_messages(me, other_user)
    return jsonify([{"id": m[0], "sender": m[1], "recipient": m[2], "message": m[3], "timestamp": m[4]} for m in msgs])

@app.route("/keys/<other_user>")
def get_keys(other_user):
    if "username" not in session:
        return jsonify({"error": "Not logged in"}), 403
    me = session["username"]
    my_data = get_user(me)
    other_data = get_user(other_user)
    if not my_data:
        return jsonify({"error": "Your user not found"}), 404
    if not other_data:
        return jsonify({"error": "Other user not found"}), 404
    p, c, seed = DEMO_PARAMS["p"], DEMO_PARAMS["c"], DEMO_PARAMS["SEED"]
    return jsonify({
        "params": {"p": p, "c": c, "SEED": seed},
        "me": {"username": me, "private_key": my_data[3], "public_key": my_data[4]},
        "other": {"username": other_user, "public_key": other_data[4]}
    })

@app.route("/delete_message/<int:msg_id>", methods=["POST"])
def delete_message(msg_id):
    if "username" not in session:
        return jsonify({"error": "Not logged in"}), 403
    me = session["username"]
    delete_message_by_id(msg_id, me)
    socketio.emit("message_deleted", {"id": msg_id})
    return jsonify({"status": "deleted"})

@app.route("/delete_group", methods=["POST"])
def delete_group():
    if "username" not in session:
        return jsonify({"error": "Not logged in"}), 403
    delete_group_messages()
    socketio.emit("group_cleared")
    return jsonify({"status": "deleted"})

@app.route("/debug")
def debug_page():
    users = get_all_users()
    messages = get_all_messages()
    return jsonify({
        "users": [{"id": u[0], "username": u[1], "private_key": u[2], "public_key": u[3]} for u in users],
        "messages": [{"id": m[0], "sender": m[1], "recipient": m[2], "message": m[3], "timestamp": m[4]} for m in messages]
    })

# ---------------- Socket events ----------------
@socketio.on("connect")
def handle_connect(auth=None):
    uname = None
    if auth and "username" in auth:
        uname = auth["username"]
    elif "username" in session:
        uname = session["username"]

    if uname:
        session["username"] = uname
        join_room(uname)
        with lock:
            clients[uname] = True
        socketio.emit("user_status", {"users": list(clients.keys())})

@socketio.on("disconnect")
def handle_disconnect():
    uname = session.get("username")
    if uname:
        with lock:
            clients.pop(uname, None)
        socketio.emit("user_status", {"users": list(clients.keys())})

@socketio.on("send_message")
def handle_send_message(data):
    sender = session.get("username")
    recipient = data.get("recipient")
    message = (data.get("message") or "").strip()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not sender or not message:
        return
    if recipient == "":
        recipient = None

    msg_id = save_message(sender, recipient, message, ts)
    payload = {"id": msg_id, "sender": sender, "recipient": recipient, "message": message, "timestamp": ts}

    if recipient:
        emit("new_message", payload, room=recipient)
        emit("new_message", payload, room=sender)
    else:
        for user in clients:
            emit("new_message", payload, room=user)

# ---------------- Startup ----------------
if __name__ == "__main__":
    os.makedirs("templates", exist_ok=True)
    os.makedirs("static", exist_ok=True)
    init_db()
    port = int(os.environ.get("PORT", 5555))  # Render provides PORT
    socketio.run(app, host="0.0.0.0", port=port)

