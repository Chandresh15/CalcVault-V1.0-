"""
auth.py — CalcVault (Ramboll Edition)
=====================================
Users, sessions, roles, and the "online now" heartbeat.

Schema (auto-created in database.db):
    users
      id, username (unique), password_hash, password_plain,
      role ('owner' | 'user'), full_name, created_at, last_seen

⚠️  password_plain is stored per the Owner's explicit requirement so
    the Owner can view/reset team passwords. Access is confined to
    owner-only routes (see `list_users_full`). Remove that column
    and `set_password`'s second write if you ever tighten security.

Bootstrap:
    On first run, an "owner" account is created:
        username: owner    password: owner    role: owner
    A "Change password" banner should appear on the owner dashboard.

Public helpers used by app.py:
    init_db(db_path)                    -- create schema + default owner
    login(username, password)           -- returns user dict or None
    logout()                            -- clears session
    current_user()                      -- session-backed dict or None
    need_login()  / need_owner()        -- Flask before-request guards
    heartbeat(user_id)                  -- updates last_seen
    online_count(minutes=2)             -- for the sidebar chip
    list_users_full()                   -- owner-only, incl. plain pw
    list_users_public()                 -- name+role only
    add_user / update_user / delete_user / set_password
"""

from __future__ import annotations
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from flask import session, redirect, url_for, flash, request, g
from werkzeug.security import generate_password_hash, check_password_hash


# ---------------------------------------------------------------
# Config
# ---------------------------------------------------------------
DEFAULT_OWNER_USERNAME = "owner"
DEFAULT_OWNER_PASSWORD = "owner"
DEFAULT_OWNER_NAME     = "Ramboll Owner"

_DB_PATH: Optional[str] = None   # set by init_db()


# ===============================================================
# DB connection (per-request via flask.g)
# ===============================================================
def _connect() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("auth.init_db() must be called before use.")
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_db() -> sqlite3.Connection:
    """Return a per-request cached connection (auto-closed by app teardown)."""
    if "db" not in g:
        g.db = _connect()
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ===============================================================
# Schema
# ===============================================================
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT    UNIQUE NOT NULL,
    password_hash  TEXT    NOT NULL,
    password_plain TEXT    NOT NULL,       -- Owner-visible (per spec)
    role           TEXT    NOT NULL CHECK(role IN ('owner','user')),
    full_name      TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL,
    last_seen      TEXT
);
CREATE INDEX IF NOT EXISTS ix_users_last_seen ON users(last_seen);
"""


def init_db(db_path: str) -> None:
    """Create schema and seed the default owner if the table is empty."""
    global _DB_PATH
    _DB_PATH = db_path

    with _connect() as c:
        c.executescript(_SCHEMA_SQL)
        row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] == 0:
            c.execute(
                """INSERT INTO users
                   (username, password_hash, password_plain, role,
                    full_name, created_at)
                   VALUES (?, ?, ?, 'owner', ?, ?)""",
                (
                    DEFAULT_OWNER_USERNAME,
                    generate_password_hash(DEFAULT_OWNER_PASSWORD),
                    DEFAULT_OWNER_PASSWORD,
                    DEFAULT_OWNER_NAME,
                    _now_iso(),
                ),
            )
        c.commit()


# ===============================================================
# Time helpers
# ===============================================================
def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ===============================================================
# Session / login
# ===============================================================
def _row_to_dict(row: sqlite3.Row, include_plain: bool = False) -> Dict[str, Any]:
    d = {
        "id":         row["id"],
        "username":   row["username"],
        "role":       row["role"],
        "full_name":  row["full_name"] or row["username"],
        "created_at": row["created_at"],
        "last_seen":  row["last_seen"],
    }
    if include_plain:
        d["password_plain"] = row["password_plain"]
    return d


def login(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Verify credentials, set session on success. Returns user dict or None."""
    if not username or not password:
        return None
    row = get_db().execute(
        "SELECT * FROM users WHERE username = ?",
        (username.strip(),),
    ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return None

    user = _row_to_dict(row)
    session.clear()
    session["uid"]  = user["id"]
    session["role"] = user["role"]
    session["name"] = user["full_name"]
    session["un"]   = user["username"]
    session.permanent = True
    heartbeat(user["id"])
    return user


def logout() -> None:
    session.clear()


def current_user() -> Optional[Dict[str, Any]]:
    """Return the logged-in user (cached per request) or None."""
    if "uid" not in session:
        return None
    if "user" not in g:
        row = get_db().execute(
            "SELECT * FROM users WHERE id = ?", (session["uid"],)
        ).fetchone()
        g.user = _row_to_dict(row) if row else None
    return g.user


# ===============================================================
# Route guards
# ===============================================================
def need_login():
    """Use as: `if (r := need_login()): return r` at top of a route."""
    if current_user() is None:
        flash("Please sign in to continue.", "warn")
        return redirect(url_for("login_view", next=request.path))
    return None


def need_owner():
    """Owner-gate. Returns a redirect Response if not owner, else None."""
    u = current_user()
    if u is None:
        flash("Please sign in to continue.", "warn")
        return redirect(url_for("login_view", next=request.path))
    if u["role"] != "owner":
        flash("Owner access required.", "error")
        return redirect(url_for("dashboard"))
    return None


# ===============================================================
# Heartbeat / online presence
# ===============================================================
def heartbeat(user_id: Optional[int] = None) -> None:
    """
    Update last_seen for the given (or current) user. Called from
    every request via app.before_request so the "online" chip stays
    accurate without extra AJAX traffic.
    """
    if user_id is None:
        u = current_user()
        if u is None:
            return
        user_id = u["id"]
    get_db().execute(
        "UPDATE users SET last_seen = ? WHERE id = ?",
        (_now_iso(), user_id),
    )
    get_db().commit()


def online_count(minutes: int = 2) -> int:
    """Users active within the last `minutes` window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)) \
             .replace(microsecond=0).isoformat()
    row = get_db().execute(
        "SELECT COUNT(*) AS n FROM users WHERE last_seen >= ?",
        (cutoff,),
    ).fetchone()
    return int(row["n"] or 0)


def online_users(minutes: int = 2) -> List[Dict[str, Any]]:
    """List of currently-online users (for tooltip on the chip)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)) \
             .replace(microsecond=0).isoformat()
    rows = get_db().execute(
        """SELECT id, username, role, full_name, last_seen
             FROM users WHERE last_seen >= ?
             ORDER BY last_seen DESC""",
        (cutoff,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ===============================================================
# User CRUD  (owner-only routes will wrap these)
# ===============================================================
def list_users_public() -> List[Dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM users ORDER BY role DESC, username ASC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_users_full() -> List[Dict[str, Any]]:
    """Includes plain password — call ONLY from owner-gated routes."""
    rows = get_db().execute(
        "SELECT * FROM users ORDER BY role DESC, username ASC"
    ).fetchall()
    return [_row_to_dict(r, include_plain=True) for r in rows]


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    row = get_db().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return _row_to_dict(row, include_plain=True) if row else None


def add_user(username: str, password: str, role: str,
             full_name: str = "") -> Dict[str, Any]:
    username  = (username  or "").strip()
    full_name = (full_name or "").strip()
    if not username:
        raise ValueError("Username is required.")
    if not password:
        raise ValueError("Password is required.")
    if role not in ("owner", "user"):
        raise ValueError("Role must be 'owner' or 'user'.")

    db = get_db()
    exists = db.execute(
        "SELECT 1 FROM users WHERE username = ?", (username,)
    ).fetchone()
    if exists:
        raise ValueError(f"Username '{username}' already exists.")

    cur = db.execute(
        """INSERT INTO users
           (username, password_hash, password_plain, role,
            full_name, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (username, generate_password_hash(password), password,
         role, full_name or username, _now_iso()),
    )
    db.commit()
    return get_user(cur.lastrowid)


def update_user(user_id: int, *, role: Optional[str] = None,
                full_name: Optional[str] = None) -> None:
    fields, values = [], []
    if role is not None:
        if role not in ("owner", "user"):
            raise ValueError("Role must be 'owner' or 'user'.")
        fields.append("role = ?");      values.append(role)
    if full_name is not None:
        fields.append("full_name = ?"); values.append(full_name.strip())
    if not fields:
        return
    values.append(user_id)
    db = get_db()
    db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
    db.commit()


def set_password(user_id: int, new_password: str) -> None:
    if not new_password:
        raise ValueError("Password cannot be empty.")
    db = get_db()
    db.execute(
        "UPDATE users SET password_hash = ?, password_plain = ? WHERE id = ?",
        (generate_password_hash(new_password), new_password, user_id),
    )
    db.commit()


def delete_user(user_id: int, acting_user_id: int) -> None:
    """
    Owner cannot delete themselves. Prevents deleting the last owner.
    """
    if user_id == acting_user_id:
        raise ValueError("You cannot delete your own account.")
    db  = get_db()
    row = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise ValueError("User not found.")
    if row["role"] == "owner":
        n = db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'owner'"
        ).fetchone()["n"]
        if n <= 1:
            raise ValueError("Cannot delete the last remaining owner.")

    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()


# ===============================================================
# Small helpers exposed to templates via app context processor
# ===============================================================
def role_of(user: Optional[Dict[str, Any]]) -> str:
    return (user or {}).get("role", "guest")


def is_owner(user: Optional[Dict[str, Any]]) -> bool:
    return role_of(user) == "owner"


# ===============================================================
# Local sanity check (no Flask context) — `python auth.py`
# ===============================================================
# ===============================================================
# Local sanity check (no Flask context) — `python auth.py`
# ===============================================================
if __name__ == "__main__":
    import os, tempfile
    tmp = os.path.join(tempfile.gettempdir(), "cv_auth_test.db")
    if os.path.exists(tmp):
        os.remove(tmp)

    # Bypass the Flask-aware _connect() by driving sqlite3 directly.
    import sqlite3
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        """INSERT INTO users (username, password_hash, password_plain,
                              role, full_name, created_at)
           VALUES (?, ?, ?, 'owner', ?, ?)""",
        ("owner", generate_password_hash("owner"), "owner",
         "Test Owner", _now_iso()),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM users").fetchone()
    assert row["username"] == "owner"
    assert check_password_hash(row["password_hash"], "owner")
    assert row["password_plain"] == "owner"

    conn.close()
    os.remove(tmp)
    print("✅ auth.py schema + password roundtrip OK")