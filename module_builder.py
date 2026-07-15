"""
module_builder.py — CalcVault (Ramboll Edition)
See module docstring above.
"""

from __future__ import annotations
import json
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Dict, Any, List

from simpleeval import SimpleEval, NameNotDefined, InvalidExpression


# ===============================================================
# Safe function + constant whitelist  (matches the cheat sheet)
# ===============================================================
FUNCTION_NAMES: Dict[str, Callable] = {
    "sqrt":  math.sqrt, "log":   math.log,   "log10": math.log10,
    "exp":   math.exp,  "sin":   math.sin,   "cos":   math.cos,
    "tan":   math.tan,  "asin":  math.asin,  "acos":  math.acos,
    "atan":  math.atan, "abs":   abs,        "min":   min,
    "max":   max,       "round": round,      "floor": math.floor,
    "ceil":  math.ceil, "pow":   math.pow,
}
CONSTANTS: Dict[str, float] = {"pi": math.pi, "e": math.e, "g": 9.81}

CHEAT_SHEET: str = (
    "Operators:  +  -  *  /  ^ (or **)     "
    "Functions:  sqrt, log, log10, exp, sin, cos, tan, "
    "asin, acos, atan, abs, min, max, round, floor, ceil, pow     "
    "Constants:  pi, e, g"
)
STATUSES = ("active", "maintenance", "disabled")

_VAR_RE  = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NAME_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ===============================================================
# Module-level state
# ===============================================================
_db_getter: Optional[Callable] = None
_eval_cache: Dict[str, SimpleEval] = {}


def _db():
    if _db_getter is None:
        raise RuntimeError("module_builder.init() not called.")
    return _db_getter()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ===============================================================
# Schema
# ===============================================================
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS modules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                TEXT    UNIQUE NOT NULL,
    name                TEXT    NOT NULL,
    icon                TEXT    NOT NULL DEFAULT '📐',
    category            TEXT    NOT NULL DEFAULT 'Custom',
    description         TEXT    NOT NULL DEFAULT '',
    status              TEXT    NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','maintenance','disabled')),
    inputs_json         TEXT    NOT NULL DEFAULT '[]',
    outputs_json        TEXT    NOT NULL DEFAULT '[]',
    eval_order_json     TEXT    NOT NULL DEFAULT '[]',
    assigned_users_json TEXT    NOT NULL DEFAULT '[]',
    version             INTEGER NOT NULL DEFAULT 1,
    created_by          INTEGER,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_modules_status ON modules(status);

CREATE TABLE IF NOT EXISTS module_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id     INTEGER NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    snapshot_json TEXT    NOT NULL,
    saved_by      INTEGER,
    saved_at      TEXT    NOT NULL,
    UNIQUE(module_id, version)
);

CREATE TABLE IF NOT EXISTS module_shares (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id   INTEGER NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    token       TEXT    UNIQUE NOT NULL,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT,
    view_count  INTEGER NOT NULL DEFAULT 0,
    max_views   INTEGER,
    created_by  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_shares_token ON module_shares(token);
"""


def init(db_getter: Callable) -> None:
    global _db_getter
    _db_getter = db_getter
    db = db_getter()
    db.executescript(_SCHEMA_SQL)
    db.commit()


# ===============================================================
# Slug helpers
# ===============================================================
def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-") or "module"


def _unique_slug(base: str, exclude_id: Optional[int] = None) -> str:
    slug, n = base, 1
    while True:
        row = _db().execute(
            "SELECT id FROM modules WHERE slug = ? AND (? IS NULL OR id <> ?)",
            (slug, exclude_id, exclude_id),
        ).fetchone()
        if row is None:
            return slug
        n += 1
        slug = f"{base}-{n}"


# ===============================================================
# Formula normalisation + validation
# ===============================================================
def _normalise_formula(expr: str) -> str:
    """Accept engineer-style '^' as power; simpleeval understands '**'."""
    return (expr or "").replace("^", "**").strip()


def _identifiers_in(expr: str) -> List[str]:
    return [m.group(1) for m in _NAME_RE.finditer(expr or "")]


def _topo_sort(output_specs: List[Dict[str, Any]],
               input_vars: List[str]) -> List[str]:
    """
    Return output variable names in dependency order.
    Raises ValueError on missing refs or cycles.
    """
    known    = set(input_vars) | set(CONSTANTS) | set(FUNCTION_NAMES)
    out_map  = {o["var"]: o for o in output_specs}
    deps: Dict[str, set] = {}

    for o in output_specs:
        ids = set(_identifiers_in(o["_formula_ast"]))
        unknown = [i for i in ids
                   if i not in known and i not in out_map]
        if unknown:
            raise ValueError(
                f"Output '{o['var']}' references unknown name(s): "
                + ", ".join(sorted(unknown)))
        # Only *output* names count as edges in the DAG
        deps[o["var"]] = {i for i in ids if i in out_map and i != o["var"]}

    order: List[str] = []
    permanent, temporary = set(), set()

    def visit(node: str) -> None:
        if node in permanent:
            return
        if node in temporary:
            raise ValueError(f"Circular reference detected involving '{node}'.")
        temporary.add(node)
        for d in deps.get(node, ()):
            visit(d)
        temporary.discard(node)
        permanent.add(node)
        order.append(node)

    for var in out_map:
        visit(var)
    return order


# ===============================================================
# Validation
# ===============================================================
def _validate_inputs(inputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    clean, seen = [], set()
    for raw in inputs or []:
        var = str(raw.get("var", "")).strip()
        if not var:
            continue
        if not _VAR_RE.match(var):
            raise ValueError(f"Invalid input variable name: '{var}'.")
        if var in CONSTANTS or var in FUNCTION_NAMES:
            raise ValueError(f"Input name '{var}' collides with a built-in.")
        if var in seen:
            raise ValueError(f"Duplicate input variable: '{var}'.")
        seen.add(var)

        try:
            default = float(raw.get("default", 0) or 0)
        except (TypeError, ValueError):
            raise ValueError(f"Default for '{var}' must be numeric.")

        clean.append({
            "var":     var,
            "label":   str(raw.get("label", var)).strip() or var,
            "unit":    str(raw.get("unit", "")).strip(),
            "default": default,
            "note":    str(raw.get("note", "")).strip(),
        })
    if not clean:
        raise ValueError("At least one input is required.")
    return clean


def _validate_outputs(outputs: List[Dict[str, Any]],
                      input_vars: List[str]) -> List[Dict[str, Any]]:
    clean, seen = [], set()
    for raw in outputs or []:
        var = str(raw.get("var", "")).strip()
        formula = str(raw.get("formula", "")).strip()
        if not var and not formula:
            continue
        if not _VAR_RE.match(var):
            raise ValueError(f"Invalid output variable name: '{var}'.")
        if var in CONSTANTS or var in FUNCTION_NAMES:
            raise ValueError(f"Output name '{var}' collides with a built-in.")
        if var in seen:
            raise ValueError(f"Duplicate output variable: '{var}'.")
        if var in input_vars:
            raise ValueError(
                f"Output '{var}' has the same name as an input.")
        if not formula:
            raise ValueError(f"Output '{var}' has an empty formula.")
        seen.add(var)

        # Try parsing the formula NOW so save-time errors are surfaced
        # to the Owner before users ever hit it.
        ast_form = _normalise_formula(formula)
        try:
            _get_evaluator(ast_form).parse(ast_form)
        except (InvalidExpression, SyntaxError) as e:
            raise ValueError(f"Formula for '{var}' is invalid: {e}")

        try:
            decimals = int(raw.get("decimals", 3))
        except (TypeError, ValueError):
            decimals = 3

        clean.append({
            "var":       var,
            "label":     str(raw.get("label", var)).strip() or var,
            "unit":      str(raw.get("unit", "")).strip(),
            "formula":   formula,           # user-facing (with ^)
            "_formula_ast": ast_form,       # eval-ready (with **)
            "decimals":  max(0, min(decimals, 6)),
            "primary":   bool(raw.get("primary", False)),
        })
    if not clean:
        raise ValueError("At least one output is required.")
    return clean


# ===============================================================
# Evaluator cache
# ===============================================================
def _get_evaluator(_formula_key: str) -> SimpleEval:
    """
    Return a SimpleEval instance configured with our whitelist.
    The `names` dict is refreshed per evaluation; the parsed AST inside
    SimpleEval gets amortised across calls thanks to simpleeval's own
    internal caching.
    """
    ev = _eval_cache.get("_shared_")
    if ev is None:
        ev = SimpleEval(functions=FUNCTION_NAMES, names={})
        _eval_cache["_shared_"] = ev
    return ev


# ===============================================================
# Row helpers
# ===============================================================
def _row_to_module(row) -> Dict[str, Any]:
    return {
        "id":              row["id"],
        "slug":            row["slug"],
        "name":            row["name"],
        "icon":            row["icon"],
        "category":        row["category"],
        "description":     row["description"],
        "status":          row["status"],
        "inputs":          json.loads(row["inputs_json"]  or "[]"),
        "outputs":         json.loads(row["outputs_json"] or "[]"),
        "eval_order":      json.loads(row["eval_order_json"] or "[]"),
        "assigned_users":  json.loads(row["assigned_users_json"] or "[]"),
        "version":         row["version"],
        "created_by":      row["created_by"],
        "created_at":      row["created_at"],
        "updated_at":      row["updated_at"],
    }


# ===============================================================
# Public: list / get
# ===============================================================
def list_modules(status: Optional[str] = None,
                 for_user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM modules"
    params: List[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY category ASC, name ASC"
    rows = [_row_to_module(r) for r in _db().execute(sql, params).fetchall()]
    if for_user_id is None:
        return rows
    # Filter by assignment: empty list = everyone
    visible = []
    for m in rows:
        if not m["assigned_users"] or for_user_id in m["assigned_users"]:
            visible.append(m)
    return visible


def get_module(module_id: int) -> Optional[Dict[str, Any]]:
    row = _db().execute("SELECT * FROM modules WHERE id = ?",
                        (module_id,)).fetchone()
    return _row_to_module(row) if row else None


def get_module_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    row = _db().execute("SELECT * FROM modules WHERE slug = ?",
                        (slug,)).fetchone()
    return _row_to_module(row) if row else None


def duplicate_check(name: str, exclude_id: Optional[int] = None) -> bool:
    row = _db().execute(
        "SELECT 1 FROM modules WHERE lower(name) = lower(?) "
        "AND (? IS NULL OR id <> ?)",
        (name.strip(), exclude_id, exclude_id),
    ).fetchone()
    return row is not None


def clean_duplicates() -> int:
    """
    Remove exact-duplicate modules (same name, same inputs, same outputs),
    keeping the oldest. Returns count deleted.
    """
    rows = _db().execute(
        "SELECT id, name, inputs_json, outputs_json "
        "FROM modules ORDER BY id ASC"
    ).fetchall()

    signature: Dict[tuple, int] = {}
    to_delete: List[int] = []
    for r in rows:
        key = (r["name"].strip().lower(),
               r["inputs_json"], r["outputs_json"])
        if key in signature:
            to_delete.append(r["id"])
        else:
            signature[key] = r["id"]

    if to_delete:
        placeholders = ",".join("?" * len(to_delete))
        db = _db()
        db.execute(f"DELETE FROM modules WHERE id IN ({placeholders})",
                   to_delete)
        db.commit()
    return len(to_delete)


# ===============================================================
# Save / delete / status
# ===============================================================
def save_module(payload: Dict[str, Any],
                actor_user_id: Optional[int],
                module_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Create-or-update. Bumps version + snapshots previous state on update.
    payload keys:
        name, icon, category, description, status,
        inputs (list), outputs (list),
        assigned_users (list[int])
    """
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("Module name is required.")

    inputs  = _validate_inputs(payload.get("inputs", []))
    outputs = _validate_outputs(payload.get("outputs", []),
                                [i["var"] for i in inputs])
    eval_order = _topo_sort(outputs, [i["var"] for i in inputs])

    status = payload.get("status", "active")
    if status not in STATUSES:
        raise ValueError(f"Invalid status: {status!r}")

    assigned = payload.get("assigned_users", []) or []
    try:
        assigned = [int(x) for x in assigned]
    except (TypeError, ValueError):
        raise ValueError("assigned_users must be integers.")

    db  = _db()
    now = _now()

    # Strip the internal _formula_ast helper from what we persist
    inputs_store  = json.dumps(inputs,  ensure_ascii=False)
    outputs_store = json.dumps(
        [{k: v for k, v in o.items() if not k.startswith("_")}
         for o in outputs],
        ensure_ascii=False,
    )
    eval_order_store = json.dumps(eval_order)
    assigned_store   = json.dumps(assigned)

    if module_id is None:
        # ---- CREATE ---------------------------------------------
        base_slug = _slugify(name)
        slug      = _unique_slug(base_slug)
        cur = db.execute(
            """INSERT INTO modules
                 (slug, name, icon, category, description, status,
                  inputs_json, outputs_json, eval_order_json,
                  assigned_users_json, version,
                  created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (slug, name,
             (payload.get("icon") or "📐").strip(),
             (payload.get("category") or "Custom").strip(),
             (payload.get("description") or "").strip(),
             status,
             inputs_store, outputs_store, eval_order_store,
             assigned_store, actor_user_id, now, now),
        )
        new_id = cur.lastrowid
        # First-version snapshot
        _snapshot(new_id, 1, actor_user_id, now)
        db.commit()
        return get_module(new_id)

    # ---- UPDATE -------------------------------------------------
    existing = get_module(module_id)
    if not existing:
        raise ValueError("Module not found.")

    _snapshot(module_id, existing["version"], actor_user_id, now,
              previous=existing)
    new_ver = existing["version"] + 1

    db.execute(
        """UPDATE modules SET
             name=?, icon=?, category=?, description=?, status=?,
             inputs_json=?, outputs_json=?, eval_order_json=?,
             assigned_users_json=?, version=?, updated_at=?
           WHERE id=?""",
        (name,
         (payload.get("icon") or existing["icon"]).strip(),
         (payload.get("category") or existing["category"]).strip(),
         (payload.get("description") or "").strip(),
         status,
         inputs_store, outputs_store, eval_order_store,
         assigned_store, new_ver, now, module_id),
    )
    db.commit()
    return get_module(module_id)


def _snapshot(module_id: int, version: int,
              actor: Optional[int], when: str,
              previous: Optional[Dict[str, Any]] = None) -> None:
    if previous is None:
        previous = get_module(module_id)
    snapshot = json.dumps({
        "name":        previous["name"],
        "icon":        previous["icon"],
        "category":    previous["category"],
        "description": previous["description"],
        "status":      previous["status"],
        "inputs":      previous["inputs"],
        "outputs":     previous["outputs"],
        "assigned_users": previous["assigned_users"],
    }, ensure_ascii=False)
    _db().execute(
        """INSERT OR IGNORE INTO module_versions
             (module_id, version, snapshot_json, saved_by, saved_at)
           VALUES (?, ?, ?, ?, ?)""",
        (module_id, version, snapshot, actor, when),
    )


def delete_module(module_id: int) -> None:
    db = _db()
    db.execute("DELETE FROM modules WHERE id = ?", (module_id,))
    db.commit()


def set_status(module_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"Invalid status: {status!r}")
    db = _db()
    db.execute("UPDATE modules SET status = ?, updated_at = ? WHERE id = ?",
               (status, _now(), module_id))
    db.commit()


# ===============================================================
# Evaluation
# ===============================================================
def evaluate(module_ref: Any, values: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run a saved module against the given input values.

    Returns:
        {
          "ok": bool,
          "results": {var: number|None, ...},
          "errors":  {var: "message", ...},
          "primary_var": str | None,
        }
    Never raises for per-formula errors — surfaces them per-output so
    the UI can show a red badge on the broken row while the rest still
    render.
    """
    module = (get_module_by_slug(module_ref)
              if isinstance(module_ref, str)
              else get_module(int(module_ref)))
    if not module:
        return {"ok": False, "results": {}, "errors": {"_": "Module not found."},
                "primary_var": None}

    if module["status"] == "disabled":
        return {"ok": False, "results": {}, "errors": {"_": "Module disabled."},
                "primary_var": None}

    # Prepare the name space
    names: Dict[str, Any] = dict(CONSTANTS)
    for spec in module["inputs"]:
        v = values.get(spec["var"], spec.get("default", 0))
        try:
            names[spec["var"]] = float(v)
        except (TypeError, ValueError):
            return {"ok": False, "results": {},
                    "errors": {spec["var"]: "Value must be numeric."},
                    "primary_var": None}

    ev = _get_evaluator("_shared_")
    ev.functions = FUNCTION_NAMES

    results: Dict[str, Any] = {}
    errors:  Dict[str, str] = {}
    out_by_var = {o["var"]: o for o in module["outputs"]}
    order = module["eval_order"] or [o["var"] for o in module["outputs"]]

    for var in order:
        spec = out_by_var.get(var)
        if not spec:
            continue
        expr = _normalise_formula(spec["formula"])
        ev.names = names
        try:
            val = ev.eval(expr)
            if isinstance(val, (int, float)) and math.isfinite(val):
                val = round(float(val), spec.get("decimals", 3))
                results[var] = val
                names[var]   = val        # let subsequent outputs use it
            else:
                errors[var] = "Non-finite result."
                results[var] = None
        except (NameNotDefined, InvalidExpression,
                ZeroDivisionError, ValueError, SyntaxError,
                TypeError, OverflowError) as e:
            errors[var]  = str(e)
            results[var] = None

    primary = next((o["var"] for o in module["outputs"] if o.get("primary")),
                   None) or (module["outputs"][0]["var"] if module["outputs"]
                             else None)

    return {"ok": not errors, "results": results, "errors": errors,
            "primary_var": primary}


# ===============================================================
# Versions / rollback
# ===============================================================
def list_versions(module_id: int) -> List[Dict[str, Any]]:
    rows = _db().execute(
        """SELECT id, version, saved_by, saved_at
             FROM module_versions
            WHERE module_id = ?
            ORDER BY version DESC""",
        (module_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def rollback_to(module_id: int, version: int,
                actor_user_id: Optional[int]) -> Dict[str, Any]:
    row = _db().execute(
        "SELECT snapshot_json FROM module_versions "
        "WHERE module_id = ? AND version = ?",
        (module_id, version),
    ).fetchone()
    if not row:
        raise ValueError(f"Version {version} not found.")
    snap = json.loads(row["snapshot_json"])
    # Save-as-update to bump the version counter and snapshot current
    return save_module({**snap}, actor_user_id, module_id=module_id)


# ===============================================================
# JSON import / export
# ===============================================================
def export_json(module_id: int) -> Dict[str, Any]:
    m = get_module(module_id)
    if not m:
        raise ValueError("Module not found.")
    # Portable subset — no ids, no created_by
    return {
        "name": m["name"], "icon": m["icon"], "category": m["category"],
        "description": m["description"], "status": m["status"],
        "inputs": m["inputs"], "outputs": m["outputs"],
        "assigned_users": [],   # never leak user IDs across environments
        "_source": "CalcVault", "_version": m["version"],
    }


def import_json(payload: Dict[str, Any],
                actor_user_id: Optional[int]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Import payload must be a JSON object.")
    return save_module({
        "name":        payload.get("name", "Imported Module"),
        "icon":        payload.get("icon", "📐"),
        "category":    payload.get("category", "Custom"),
        "description": payload.get("description", ""),
        "status":      payload.get("status", "active"),
        "inputs":      payload.get("inputs",  []),
        "outputs":     payload.get("outputs", []),
        "assigned_users": [],
    }, actor_user_id)


# ===============================================================
# Public share tokens
# ===============================================================
def create_share(module_id: int,
                 ttl_hours: Optional[int] = None,
                 max_views: Optional[int] = None,
                 actor: Optional[int] = None) -> Dict[str, Any]:
    if not get_module(module_id):
        raise ValueError("Module not found.")
    token = secrets.token_urlsafe(16)
    expires = None
    if ttl_hours and ttl_hours > 0:
        expires = (datetime.now(timezone.utc)
                   + timedelta(hours=int(ttl_hours))).replace(microsecond=0)\
                    .isoformat()
    db  = _db()
    cur = db.execute(
        """INSERT INTO module_shares
             (module_id, token, created_at, expires_at,
              view_count, max_views, created_by)
           VALUES (?, ?, ?, ?, 0, ?, ?)""",
        (module_id, token, _now(), expires,
         int(max_views) if max_views else None, actor),
    )
    db.commit()
    return {"id": cur.lastrowid, "token": token,
            "expires_at": expires, "max_views": max_views}


def resolve_share(token: str) -> Optional[Dict[str, Any]]:
    """
    Return the module dict for a valid token, incrementing view_count.
    Returns None if the token is unknown, expired, or exhausted.
    """
    if not token:
        return None
    row = _db().execute(
        "SELECT * FROM module_shares WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return None
    if row["expires_at"]:
        try:
            if datetime.fromisoformat(row["expires_at"]) < \
               datetime.now(timezone.utc):
                return None
        except ValueError:
            return None
    if row["max_views"] and row["view_count"] >= row["max_views"]:
        return None

    db = _db()
    db.execute(
        "UPDATE module_shares SET view_count = view_count + 1 WHERE id = ?",
        (row["id"],),
    )
    db.commit()

    module = get_module(row["module_id"])
    if module:
        module["_share_meta"] = {
            "view_count": row["view_count"] + 1,
            "max_views":  row["max_views"],
            "expires_at": row["expires_at"],
        }
    return module


def revoke_share(share_id: int) -> None:
    db = _db()
    db.execute("DELETE FROM module_shares WHERE id = ?", (share_id,))
    db.commit()


def list_shares(module_id: int) -> List[Dict[str, Any]]:
    rows = _db().execute(
        "SELECT * FROM module_shares WHERE module_id = ? "
        "ORDER BY created_at DESC", (module_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ===============================================================
# Local smoke test
# ===============================================================
if __name__ == "__main__":
    import sqlite3, tempfile, os
    tmp = os.path.join(tempfile.gettempdir(), "cv_mb_test.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    init(lambda: conn)

    m = save_module({
        "name": "Pipe area & velocity", "icon": "📏",
        "inputs": [
            {"var": "d_mm", "label": "Diameter", "unit": "mm", "default": 100},
            {"var": "q_m3h","label": "Flow",     "unit": "m³/hr","default": 84.8},
        ],
        "outputs": [
            {"var": "d_m", "label": "Diameter",  "unit": "m",
             "formula": "d_mm/1000", "decimals": 4},
            {"var": "area","label": "Area",      "unit": "m²",
             "formula": "pi*d_m^2/4", "decimals": 6},
            {"var": "v",   "label": "Velocity",  "unit": "m/s",
             "formula": "(q_m3h/3600)/area", "decimals": 3,
             "primary": True},
        ],
    }, actor_user_id=1)

    out = evaluate(m["id"], {"d_mm": 100, "q_m3h": 84.8})
    print("results:", out["results"], "errors:", out["errors"])
    assert abs(out["results"]["v"] - 3.0) < 0.01
    assert out["primary_var"] == "v"

    # Share
    s = create_share(m["id"], ttl_hours=1, max_views=2, actor=1)
    assert resolve_share(s["token"])["id"] == m["id"]
    assert resolve_share(s["token"])["id"] == m["id"]
    assert resolve_share(s["token"]) is None   # max_views exhausted

    # Duplicate detection
    assert duplicate_check("Pipe area & velocity")

    print("✅ module_builder OK")
    os.remove(tmp)