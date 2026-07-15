"""
pump_databank.py — CalcVault (Ramboll Edition)
==============================================
Owner-managed reference-pump library (read-only for users).

Schema (auto-created in database.db):
    pumps
      id, vendor, model, flow_m3h, head_m,
      pump_eff_pct, motor_eff_pct, motor_kw,
      pump_weight_kg, motor_weight_kg, notes,
      pdf_filename (server-side sanitised name in uploads/pump_pdfs/),
      pdf_original (original client filename, used for download),
      created_at, updated_at

Public API:
    init(db_getter, upload_dir)         -- register schema + upload path
    list_pumps(search=None, sort=None, order='asc')
    get_pump(pump_id)
    create_pump(payload, pdf_stream=None, pdf_filename=None)
    update_pump(pump_id, payload, pdf_stream=None, pdf_filename=None)
    delete_pump(pump_id)
    suggest_for_duty(flow_m3h, head_m, tolerance_pct=25, limit=5)
    pdf_path(pump_id)                   -- absolute file path for send_file
    ALLOWED_PDF_EXT
"""

from __future__ import annotations
import os
import re
import uuid
import shutil
from datetime import datetime, timezone
from typing import Callable, Optional, List, Dict, Any, BinaryIO


ALLOWED_PDF_EXT = {".pdf"}
MAX_PDF_BYTES   = 15 * 1024 * 1024      # 15 MB — plenty for a spec sheet

_db_getter: Optional[Callable] = None    # set by init()
_upload_dir: Optional[str]     = None


# ===============================================================
# Init  (wired from app.py at startup)
# ===============================================================
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pumps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor            TEXT    NOT NULL DEFAULT '',
    model             TEXT    NOT NULL DEFAULT '',
    flow_m3h          REAL    NOT NULL DEFAULT 0,
    head_m            REAL    NOT NULL DEFAULT 0,
    pump_eff_pct      REAL    NOT NULL DEFAULT 0,
    motor_eff_pct     REAL    NOT NULL DEFAULT 0,
    motor_kw          REAL    NOT NULL DEFAULT 0,
    pump_weight_kg    REAL    NOT NULL DEFAULT 0,
    motor_weight_kg   REAL    NOT NULL DEFAULT 0,
    notes             TEXT    NOT NULL DEFAULT '',
    pdf_filename      TEXT,
    pdf_original      TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pumps_vendor ON pumps(vendor);
CREATE INDEX IF NOT EXISTS ix_pumps_duty   ON pumps(flow_m3h, head_m);
"""


def init(db_getter: Callable, upload_dir: str) -> None:
    """
    Called once from app.py.
      db_getter: a zero-arg callable returning the per-request sqlite3.Connection
                 (use auth.get_db so both modules share the same connection)
      upload_dir: absolute path where PDFs live (usually .../uploads/pump_pdfs)
    """
    global _db_getter, _upload_dir
    _db_getter  = db_getter
    _upload_dir = upload_dir
    os.makedirs(upload_dir, exist_ok=True)

    db = db_getter()
    db.executescript(_SCHEMA_SQL)
    db.commit()


def _db():
    if _db_getter is None:
        raise RuntimeError("pump_databank.init() was not called.")
    return _db_getter()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ===============================================================
# Row helpers
# ===============================================================
_NUM_FIELDS = (
    "flow_m3h", "head_m", "pump_eff_pct", "motor_eff_pct",
    "motor_kw", "pump_weight_kg", "motor_weight_kg",
)
_TXT_FIELDS = ("vendor", "model", "notes")


def _row_to_dict(row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _coerce_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate + normalise an incoming form payload.
    Non-negative numerics; strings stripped; efficiencies clamped.
    """
    out: Dict[str, Any] = {}

    for k in _TXT_FIELDS:
        out[k] = str(payload.get(k, "") or "").strip()

    if not out["vendor"] and not out["model"]:
        raise ValueError("Enter at least a vendor or model.")

    for k in _NUM_FIELDS:
        v = payload.get(k, 0)
        if v in (None, ""):
            v = 0
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k.replace('_', ' ').title()} must be numeric.")
        if v < 0:
            raise ValueError(f"{k.replace('_', ' ').title()} cannot be negative.")
        out[k] = v

    for k in ("pump_eff_pct", "motor_eff_pct"):
        if out[k] > 100:
            raise ValueError(f"{k.replace('_', ' ').title()} must be ≤ 100.")

    return out


# ===============================================================
# PDF file handling
# ===============================================================
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitise_name(name: str) -> str:
    name = os.path.basename((name or "").strip())
    return _SAFE_NAME_RE.sub("_", name) or "spec.pdf"


def _store_pdf(stream: BinaryIO, original_name: str) -> tuple[str, str]:
    """
    Persist an uploaded PDF. Returns (stored_filename, original_display_name).
    """
    original = _sanitise_name(original_name)
    ext      = os.path.splitext(original)[1].lower()
    if ext not in ALLOWED_PDF_EXT:
        raise ValueError("Only .pdf files are accepted.")

    # UUID prefix → collision-proof + prevents traversal even if
    # _sanitise_name ever misses something
    stored = f"{uuid.uuid4().hex[:12]}_{original}"
    dest   = os.path.join(_upload_dir, stored)

    # Enforce size while writing (stream may not report size in advance)
    written = 0
    with open(dest, "wb") as fh:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_PDF_BYTES:
                fh.close()
                os.remove(dest)
                raise ValueError(
                    f"PDF exceeds {MAX_PDF_BYTES // (1024 * 1024)} MB limit."
                )
            fh.write(chunk)

    if written == 0:
        os.remove(dest)
        raise ValueError("Uploaded PDF is empty.")

    return stored, original


def _delete_stored_pdf(stored_filename: Optional[str]) -> None:
    if not stored_filename:
        return
    path = os.path.join(_upload_dir, stored_filename)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        # Non-fatal — leaving a stray file is safer than crashing the request
        pass


def pdf_path(pump_id: int) -> Optional[tuple[str, str]]:
    """
    Return (absolute_path, download_filename) for the pump's spec sheet,
    or None if no PDF is attached / the file is missing on disk.
    """
    row = _db().execute(
        "SELECT pdf_filename, pdf_original FROM pumps WHERE id = ?",
        (pump_id,),
    ).fetchone()
    if not row or not row["pdf_filename"]:
        return None
    abs_path = os.path.join(_upload_dir, row["pdf_filename"])
    if not os.path.isfile(abs_path):
        return None
    return abs_path, (row["pdf_original"] or row["pdf_filename"])


# ===============================================================
# Read
# ===============================================================
_SORT_WHITELIST = {
    "vendor", "model", "flow_m3h", "head_m",
    "pump_eff_pct", "motor_eff_pct", "motor_kw", "updated_at",
}


def list_pumps(search: Optional[str] = None,
               sort: Optional[str] = "vendor",
               order: str = "asc") -> List[Dict[str, Any]]:
    """Search + sort. `search` matches vendor / model / notes (case-insensitive)."""
    sort = sort if sort in _SORT_WHITELIST else "vendor"
    order = "DESC" if str(order).lower() == "desc" else "ASC"

    sql    = "SELECT * FROM pumps"
    params: List[Any] = []
    if search:
        sql += (" WHERE lower(vendor) LIKE ? OR lower(model) LIKE ?"
                " OR lower(notes) LIKE ?")
        needle = f"%{search.strip().lower()}%"
        params = [needle, needle, needle]
    sql += f" ORDER BY {sort} {order}, id ASC"

    rows = _db().execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_pump(pump_id: int) -> Optional[Dict[str, Any]]:
    row = _db().execute(
        "SELECT * FROM pumps WHERE id = ?", (pump_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


# ===============================================================
# Create / update / delete
# ===============================================================
def create_pump(payload: Dict[str, Any],
                pdf_stream: Optional[BinaryIO] = None,
                pdf_filename: Optional[str] = None) -> Dict[str, Any]:
    data  = _coerce_payload(payload)
    now   = _now()

    stored = original = None
    if pdf_stream is not None and pdf_filename:
        stored, original = _store_pdf(pdf_stream, pdf_filename)

    db  = _db()
    cur = db.execute(
        """INSERT INTO pumps
           (vendor, model, flow_m3h, head_m, pump_eff_pct, motor_eff_pct,
            motor_kw, pump_weight_kg, motor_weight_kg, notes,
            pdf_filename, pdf_original, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["vendor"], data["model"],
            data["flow_m3h"], data["head_m"],
            data["pump_eff_pct"], data["motor_eff_pct"],
            data["motor_kw"], data["pump_weight_kg"], data["motor_weight_kg"],
            data["notes"], stored, original, now, now,
        ),
    )
    db.commit()
    return get_pump(cur.lastrowid)


def update_pump(pump_id: int,
                payload: Dict[str, Any],
                pdf_stream: Optional[BinaryIO] = None,
                pdf_filename: Optional[str] = None,
                remove_pdf: bool = False) -> Dict[str, Any]:
    existing = get_pump(pump_id)
    if not existing:
        raise ValueError("Pump not found.")
    data = _coerce_payload(payload)

    # PDF handling
    stored   = existing["pdf_filename"]
    original = existing["pdf_original"]
    if remove_pdf:
        _delete_stored_pdf(existing["pdf_filename"])
        stored = original = None
    if pdf_stream is not None and pdf_filename:
        # replace: delete the old one first
        _delete_stored_pdf(existing["pdf_filename"])
        stored, original = _store_pdf(pdf_stream, pdf_filename)

    db = _db()
    db.execute(
        """UPDATE pumps SET
             vendor=?, model=?, flow_m3h=?, head_m=?,
             pump_eff_pct=?, motor_eff_pct=?, motor_kw=?,
             pump_weight_kg=?, motor_weight_kg=?, notes=?,
             pdf_filename=?, pdf_original=?, updated_at=?
           WHERE id=?""",
        (
            data["vendor"], data["model"], data["flow_m3h"], data["head_m"],
            data["pump_eff_pct"], data["motor_eff_pct"], data["motor_kw"],
            data["pump_weight_kg"], data["motor_weight_kg"], data["notes"],
            stored, original, _now(), pump_id,
        ),
    )
    db.commit()
    return get_pump(pump_id)


def delete_pump(pump_id: int) -> None:
    existing = get_pump(pump_id)
    if not existing:
        return
    _delete_stored_pdf(existing["pdf_filename"])
    db = _db()
    db.execute("DELETE FROM pumps WHERE id = ?", (pump_id,))
    db.commit()


# ===============================================================
# Suggest reference pumps for a given duty point
# (feeds the "📌 Use this pump" side panel on the Pump Power page)
# ===============================================================
def suggest_for_duty(flow_m3h: float, head_m: float,
                     tolerance_pct: float = 25.0,
                     limit: int = 5) -> List[Dict[str, Any]]:
    """
    Rank reference pumps by how close their duty point is to (flow, head).
    Uses a normalised Euclidean distance so flow (typically 10–1000) doesn't
    dominate head (typically 1–100).
    """
    try:
        q = float(flow_m3h); h = float(head_m)
    except (TypeError, ValueError):
        return []
    if q <= 0 or h <= 0:
        return []

    rows = _db().execute(
        "SELECT * FROM pumps WHERE flow_m3h > 0 AND head_m > 0"
    ).fetchall()

    scored: List[tuple[float, Dict[str, Any]]] = []
    for r in rows:
        df = (r["flow_m3h"] - q) / q
        dh = (r["head_m"]   - h) / h
        # Skip pumps way outside tolerance for either axis
        if abs(df) * 100 > tolerance_pct or abs(dh) * 100 > tolerance_pct:
            continue
        score = (df * df + dh * dh) ** 0.5
        pump  = _row_to_dict(r)
        pump["match_score"] = round(score, 4)
        pump["deviation"]   = {
            "flow_pct": round(df * 100, 1),
            "head_pct": round(dh * 100, 1),
        }
        scored.append((score, pump))

    scored.sort(key=lambda x: x[0])
    return [p for _, p in scored[:limit]]


# ===============================================================
# Bulk maintenance  (owner utility, called from admin route)
# ===============================================================
def orphan_scan() -> List[str]:
    """
    Return a list of PDF files in upload_dir that no pump row references.
    Owner may use this from a maintenance page to clean disk clutter.
    """
    if not _upload_dir or not os.path.isdir(_upload_dir):
        return []
    referenced = {
        r["pdf_filename"] for r in _db().execute(
            "SELECT pdf_filename FROM pumps WHERE pdf_filename IS NOT NULL"
        )
    }
    on_disk = set(os.listdir(_upload_dir))
    return sorted(on_disk - referenced)


def purge_orphans() -> int:
    """Delete orphaned PDFs. Returns the count removed."""
    removed = 0
    for name in orphan_scan():
        try:
            os.remove(os.path.join(_upload_dir, name))
            removed += 1
        except OSError:
            pass
    return removed


# ===============================================================
# Simple local smoke test — `python pump_databank.py`
# ===============================================================
if __name__ == "__main__":
    import sqlite3, tempfile

    tmpdir = tempfile.mkdtemp(prefix="cv_pump_")
    db_path = os.path.join(tmpdir, "test.db")
    upload  = os.path.join(tmpdir, "pdfs")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init(lambda: conn, upload)

    p1 = create_pump({
        "vendor": "Grundfos", "model": "NB 80-200",
        "flow_m3h": 212, "head_m": 30,
        "pump_eff_pct": 78, "motor_eff_pct": 94, "motor_kw": 30,
        "pump_weight_kg": 320, "motor_weight_kg": 180,
        "notes": "Reference — DN80 discharge",
    })
    p2 = create_pump({
        "vendor": "Xylem", "model": "e-NSC 100-250",
        "flow_m3h": 200, "head_m": 32,
        "pump_eff_pct": 76, "motor_eff_pct": 93, "motor_kw": 30,
        "notes": "",
    })
    p3 = create_pump({
        "vendor": "KSB",   "model": "Etanorm 65",
        "flow_m3h":  50,   "head_m": 12,
        "pump_eff_pct": 70, "motor_eff_pct": 90, "motor_kw": 3,
    })

    assert len(list_pumps()) == 3
    assert len(list_pumps(search="grund")) == 1

    matches = suggest_for_duty(210, 30)
    assert matches and matches[0]["vendor"] in ("Grundfos", "Xylem")
    print(f"✅ pump_databank OK — best match: "
          f"{matches[0]['vendor']} {matches[0]['model']} "
          f"(score {matches[0]['match_score']})")

    shutil.rmtree(tmpdir, ignore_errors=True)