"""
app.py — CalcVault (Ramboll Edition)
====================================
Flask app entrypoint. Wires auth, calculations, PDF, Excel,
pump databank, and the module builder together.

Run:   python app.py                    (auto-picks Waitress if installed)
       waitress-serve --port=5000 app:app
"""

from __future__ import annotations
import io
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session,
    send_file, jsonify, abort, Response,
)

import auth
import calculations as calc
import pump_databank as pdb
import module_builder as mb
import excel_export
import excel_import
import pdf_generator
import units as U


# ===============================================================
# App setup
# ===============================================================
APP_ROOT   = os.path.abspath(os.path.dirname(__file__))
DB_PATH    = os.path.join(APP_ROOT, "database.db")
UPLOAD_DIR = os.path.join(APP_ROOT, "uploads")
PUMP_PDFS  = os.path.join(UPLOAD_DIR, "pump_pdfs")
os.makedirs(PUMP_PDFS, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY = os.environ.get("CV_SECRET",
                                "ramboll-calcvault-local-dev-key"),
    PERMANENT_SESSION_LIFETIME = timedelta(hours=10),
    MAX_CONTENT_LENGTH         = 20 * 1024 * 1024,   # 20 MB cap
    JSON_SORT_KEYS             = False,
)

# --- init database + sub-modules -------------------------------
auth.init_db(DB_PATH)
with app.app_context():
    pdb.init(auth.get_db, PUMP_PDFS)
    mb.init(auth.get_db)
    _db = auth.get_db()
    _db.executescript("""
    CREATE TABLE IF NOT EXISTS calculations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        module_slug TEXT NOT NULL,
        module_name TEXT NOT NULL,
        module_icon TEXT DEFAULT '',
        inputs_json TEXT NOT NULL DEFAULT '{}',
        results_json TEXT NOT NULL DEFAULT '{}',
        formula TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft'
               CHECK(status IN ('draft','pending','approved','rejected')),
        report_id TEXT,
        review_comment TEXT DEFAULT '',
        reviewed_by INTEGER,
        reviewed_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_calc_user   ON calculations(user_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS ix_calc_status ON calculations(status);
    CREATE INDEX IF NOT EXISTS ix_calc_module ON calculations(module_slug);

    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'info',
        title TEXT NOT NULL,
        message TEXT DEFAULT '',
        link TEXT DEFAULT '',
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_notif_user
        ON notifications(user_id, is_read, created_at DESC);
    """)
    _db.commit()

app.teardown_appcontext(auth.close_db)

# ---------- Jinja globals for base.html --------------------------------
@app.context_processor
def _inject_globals():
    u = auth.current_user()
    return dict(
        current_user       = u,
        is_owner           = auth.is_owner,
        online_count       = auth.online_count,
        HYDRAULIC_MODULES  = HYDRAULIC_MODULES,
        custom_modules     = (mb.list_modules(status="active", for_user_id=u["id"])
                              if u else []),
    )

# ===============================================================
# Shared helpers  (single source of truth per your spec)
# ===============================================================
def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v in (None, ""):
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _store_last(module_slug: str, module_name: str, module_icon: str,
                inputs: Dict[str, Any], results: Dict[str, Any],
                formula: str = "") -> Optional[int]:
    """Persist a run as a draft in the current user's history."""
    u = auth.current_user()
    if not u:
        return None
    db  = auth.get_db()
    cur = db.execute(
        """INSERT INTO calculations
             (user_id, module_slug, module_name, module_icon,
              inputs_json, results_json, formula, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
        (u["id"], module_slug, module_name, module_icon,
         json.dumps(inputs,  default=str),
         json.dumps(results, default=str),
         formula, _now()),
    )
    db.commit()
    return cur.lastrowid


log_calc     = _store_last               # alias per spec
need_login   = auth.need_login           # re-export for template routes
need_owner   = auth.need_owner


def push_notification(user_id: int, title: str, message: str = "",
                      kind: str = "info", link: str = "") -> None:
    db = auth.get_db()
    db.execute(
        """INSERT INTO notifications
             (user_id, kind, title, message, link, is_read, created_at)
           VALUES (?, ?, ?, ?, ?, 0, ?)""",
        (user_id, kind, title, message, link, _now()),
    )
    db.commit()


def _get_calc(calc_id: int) -> Optional[Dict[str, Any]]:
    row = auth.get_db().execute(
        "SELECT * FROM calculations WHERE id = ?", (calc_id,)
    ).fetchone()
    if not row:
        return None
    d = {k: row[k] for k in row.keys()}
    d["inputs"]  = json.loads(row["inputs_json"]  or "{}")
    d["results"] = json.loads(row["results_json"] or "{}")
    return d


def _can_touch_calc(row: Dict[str, Any]) -> bool:
    u = auth.current_user()
    return bool(u and (u["role"] == "owner" or row["user_id"] == u["id"]))


# ===============================================================
# Hydraulic module configuration  (single dispatch)
# Each entry drives ONE shared template + ONE shared route.
# ---------------------------------------------------------------
# tuple:  (var, label, unit, default, quantity_for_unit_picker | None)
# ===============================================================
HYDRAULIC_MODULES: Dict[str, Dict[str, Any]] = {
    "pipe-diameter": {
        "id": 1, "name": "Pipe Diameter Sizing", "icon": "📏",
        "category": "Pipes", "fn": calc.m1_pipe_diameter,
        "inputs": [
            ("flow_m3h",    "Flow (Q)",                "m³/hr", 40,   "flow"),
            ("velocity_ms", "Considered Velocity (V)", "m/s",   1.62, "velocity"),
        ],
        "outputs": [
            ("required_id_mm",  "Required ID",             "mm",  True),
            ("provided_id_mm",  "Provided ID (nearest DN)","mm",  False),
            ("actual_area_m2",  "Actual Area",             "m²",  False),
            ("actual_velocity", "Actual Velocity",         "m/s", False),
        ],
        "formula": "d = sqrt(4·Q / (pi·V))",
    },
    "pipe-head-loss": {
        "id": 2, "name": "Pipe Head Loss (Hazen-Williams)", "icon": "💧",
        "category": "Pipes", "fn": calc.m2_pipe_head_loss,
        "inputs": [
            ("flow_m3h",   "Flow (Q)",       "m³/hr", 212,   "flow"),
            ("c_factor",   "C-factor",       "—",     140,   None),
            ("pipe_id_mm", "Pipe ID",        "mm",    210.1, "length"),
            ("length_m",   "Pipe length",    "m",     131,   "length"),
        ],
        "outputs": [
            ("head_loss_m",       "Head Loss",        "m",    True),
            ("gradient_m_per_m",  "Gradient",         "m/m",  False),
            ("velocity_ms",       "Velocity in pipe", "m/s",  False),
            ("pipe_area_m2",      "Cross-section",    "m²",   False),
        ],
        "formula": "H = (Q / (1000.8·C·d_h^2.63))^1.852 · L",
        "reference": {"kind": "c_factor", "table": calc.HAZEN_C_TABLE},
    },
    "flow-through-pipe": {
        "id": 3, "name": "Flow Through Pipe", "icon": "🚰",
        "category": "Pipes", "fn": calc.m3_flow_through_pipe,
        "inputs": [
            ("dia_mm",      "Pipe Diameter",           "mm",  100, "length"),
            ("velocity_ms", "Considered Velocity (V)", "m/s", 3,   "velocity"),
        ],
        "outputs": [
            ("area_m2",         "Cross-section Area", "m²",    False),
            ("flow_m3h",        "Flow",               "m³/hr", True),
            ("flow_ls",         "Flow",               "L/s",   False),
            ("actual_velocity", "Actual Velocity",    "m/s",   False),
        ],
        "formula": "Q = A · V = (pi·d^2 / 4) · V",
    },
    "channel-sizing": {
        "id": 4, "name": "Channel Sizing", "icon": "🌊",
        "category": "Channels", "fn": calc.m4_channel_sizing,
        "inputs": [
            ("flow_m3h",        "Flow (Q)",                "m³/hr", 212, "flow"),
            ("velocity_ms",     "Considered Velocity (V)", "m/s",   0.6, "velocity"),
            ("liquid_depth_mm", "Liquid Depth",            "mm",    300, "length"),
        ],
        "outputs": [
            ("required_width_m", "Required Width",  "m",   True),
            ("provided_width_m", "Provided Width",  "m",   False),
            ("actual_area_m2",   "Actual Area",     "m²",  False),
            ("actual_velocity",  "Actual Velocity", "m/s", False),
        ],
        "formula": "W = Q / (V · d)",
    },
    "channel-head-loss": {
        "id": 5, "name": "Channel Head Loss (Manning)", "icon": "〽️",
        "category": "Channels", "fn": calc.m5_channel_head_loss,
        "inputs": [
            ("flow_m3h",       "Flow (Q)",        "m³/hr", 212,   "flow"),
            ("width_m",        "Width",           "m",     0.35,  "length"),
            ("liquid_depth_m", "Liquid Depth",    "m",     0.30,  "length"),
            ("length_m",       "Length",          "m",     100,   "length"),
            ("n_kutter",       "Kutter's n",      "—",     0.015, None),
        ],
        "outputs": [
            ("head_loss_m",         "Total Head Loss",   "m",   True),
            ("area_m2",             "Area",              "m²",  False),
            ("wetted_perimeter_m",  "Wetted Perimeter",  "m",   False),
            ("hydraulic_radius_m",  "Hydraulic Radius",  "m",   False),
            ("velocity_ms",         "Velocity",          "m/s", False),
        ],
        "formula": "H = (Q·n / (A·R^(2/3)))^2 · L",
        "reference": {"kind": "n_kutter", "table": calc.KUTTER_N_TABLE},
    },
    "tank-volume": {
        "id": 6, "name": "Circular Tank Volume", "icon": "🛢️",
        "category": "Tanks", "fn": calc.m6_tank_volume,
        "inputs": [
            ("diameter_m",     "Tank Diameter",         "m", 5,   "length"),
            ("total_height_m", "Total Height",          "m", 10,  "length"),
            ("freeboard_m",    "Free Board + Dead End", "m", 0.5, "length"),
        ],
        "outputs": [
            ("total_volume_m3",  "Total Volume",     "m³", True),
            ("effective_vol_m3", "Effective Volume", "m³", False),
            ("cs_area_m2",       "C/S Area",         "m²", False),
            ("effective_height", "Effective Height", "m",  False),
        ],
        "formula": "V = (pi·D^2 / 4) · H",
    },
    "liquid-height": {
        "id": 7, "name": "Liquid Height in Tank", "icon": "📐",
        "category": "Tanks", "fn": calc.m7_liquid_height,
        "inputs": [
            ("volume_m3",      "Total Volume",          "m³", 300, "volume"),
            ("diameter_m",     "Diameter",              "m",  7,   "length"),
            ("total_height_m", "Total Height",          "m",  7.8, "length"),
            ("freeboard_m",    "Free Board + Dead End", "m",  0.5, "length"),
        ],
        "outputs": [
            ("effective_height_mm", "Effective Height", "mm", True),
            ("effective_height_m",  "Effective Height", "m",  False),
            ("effective_vol_m3",    "Effective Volume", "m³", False),
            ("cs_area_m2",          "C/S Area",         "m²", False),
        ],
        "formula": "H_eff = (V / A) − FB",
    },
    "tank-diameter": {
        "id": 8, "name": "Tank Diameter Sizing", "icon": "⭕",
        "category": "Tanks", "fn": calc.m8_tank_diameter,
        "inputs": [
            ("volume_m3",      "Total Volume", "m³", 196.3, "volume"),
            ("total_height_m", "Total Height", "m",  10,    "length"),
        ],
        "outputs": [
            ("diameter_m", "Diameter", "m",  True),
            ("cs_area_m2", "C/S Area", "m²", False),
        ],
        "formula": "D = sqrt(4·A / pi)   where A = V/H",
    },
    "bell-mouth": {
        "id": 9, "name": "Bell-Mouth Entry Head Loss", "icon": "🔔",
        "category": "Fittings", "fn": calc.m9_bellmouth,
        "inputs": [
            ("flow_m3h",        "Flow (Q)",             "m³/hr", 212, "flow"),
            ("bell_diameter_m", "Bell-Mouth Diameter",  "m",     2,   "length"),
        ],
        "outputs": [
            ("head_loss_m",     "Head Loss",     "m", True),
            ("circumference_m", "Circumference", "m", False),
        ],
        "formula": "H = (Q / (1.84·L))^0.666    where L = pi·D",
    },
    "weir": {
        "id": 10, "name": "Rectangular Weir Head Loss", "icon": "🌀",
        "category": "Fittings", "fn": calc.m10_weir,
        "inputs": [
            ("flow_m3h",     "Flow (Q)",   "m³/hr", 212, "flow"),
            ("weir_length_m","Weir Length","m",     2,   "length"),
        ],
        "outputs": [
            ("head_loss_m", "Head Loss Over Weir", "m",    True),
            ("flow_m3s",    "Flow",                "m³/s", False),
        ],
        "formula": "H = 0.467 · (Q / L)^0.666    (Q in m³/s)",
    },
    "pump-power": {
        "id": 12, "name": "Pump Power Calculator", "icon": "⚡",
        "category": "Pump", "fn": calc.m12_pump_power,
        "inputs": [
            ("flow_m3h",       "Flow (Q)",            "m³/hr", 100,  "flow"),
            ("head_m",         "Total Head (H)",      "m",      30,  "length"),
            ("pump_eff_pct",   "Pump Efficiency (η_p)","%",     75,  None),
            ("motor_eff_pct",  "Motor Efficiency (η_m)","%",    92,  None),
            ("density_kg_m3",  "Fluid Density (ρ)",   "kg/m³", 1000, None),
        ],
        "outputs": [
            ("hydraulic_power_kw",    "Hydraulic Power (P_hyd)",  "kW",     True),
            ("shaft_power_kw",        "Shaft Power (P_shaft)",    "kW",     False),
            ("motor_input_kw",        "Motor Input Power (P_in)", "kW",     False),
            ("recommended_motor_kw",  "Recommended IEC Motor",    "kW",     False),
            ("specific_energy_kwh_m3","Specific Energy",          "kWh/m³", False),
        ],
        "formula": ("P_hyd = ρ·g·Q·H     ·     "
                    "P_shaft = P_hyd / η_p     ·     "
                    "P_input = P_shaft / η_m"),
        "reference": {
            "kind": "fluid_density",
            "table": [
                {"material": "Water (fresh, 20 °C)",     "c": 1000},
                {"material": "Water (sea, 20 °C)",       "c": 1025},
                {"material": "Sludge (thin, 2% DS)",     "c": 1010},
                {"material": "Sludge (thick, 6% DS)",    "c": 1040},
                {"material": "Diesel",                   "c": 830},
                {"material": "Glycol (50/50 water mix)", "c": 1067},
            ],
        },
        "show_pump_suggest": True,
    },
    "pump-affinity": {
        "id": 13, "name": "Pump Affinity Laws", "icon": "⚙️",
        "category": "Pump", "fn": calc.m13_pump_affinity,
        "inputs": [
            ("flow_m3h_1",   "Original Flow (Q₁)",       "m³/hr", 100,  "flow"),
            ("head_m_1",     "Original Head (H₁)",       "m",      30,  "length"),
            ("power_kw_1",   "Original Power (P₁)",      "kW",     15,  None),
            ("speed_rpm_1",  "Original Speed (N₁)",      "rpm",  1450,  None),
            ("speed_rpm_2",  "New Speed (N₂)",           "rpm",  1750,  None),
            ("impeller_mm_1","Original Impeller (D₁)",   "mm",    200,  "length"),
            ("impeller_mm_2","New Impeller (D₂)",        "mm",    200,  "length"),
        ],
        "outputs": [
            ("new_flow_m3h",   "New Flow (Q₂)",           "m³/hr", True),
            ("new_head_m",     "New Head (H₂)",           "m",     False),
            ("new_power_kw",   "New Power (P₂)",          "kW",    False),
            ("speed_ratio",    "Speed ratio (N₂/N₁)",     "—",     False),
            ("diameter_ratio", "Diameter ratio (D₂/D₁)",  "—",     False),
        ],
        "formula": ("Q₂/Q₁ = (N₂/N₁)·(D₂/D₁)     ·     "
                    "H₂/H₁ = (N₂/N₁)²·(D₂/D₁)²     ·     "
                    "P₂/P₁ = (N₂/N₁)³·(D₂/D₁)³"),
    },

    "blower-power": {
        "id": 14, "name": "Air Blower / Compressor Power", "icon": "💨",
        "category": "Blower", "fn": calc.m14_blower_power,
        "inputs": [
            ("flow_nm3h",         "Normal Flow (Q)",        "Nm³/hr",  1000, "flow"),
            ("p_suction_kpa_a",   "Suction Pressure (p₁)",  "kPa_a", 101.325, None),
            ("p_discharge_kpa_a", "Discharge Pressure (p₂)","kPa_a",  150,   None),
            ("temp_suction_c",    "Suction Temperature (T₁)","°C",     20,   None),
            ("k_ratio",           "Ratio of specific heats (k)","—",   1.4,  None),
            ("gas_mw",            "Gas molecular weight (MW)","kg/kmol",29,  None),
            ("eta_adiabatic_pct", "Adiabatic Efficiency",   "%",       70,   None),
            ("eta_motor_pct",     "Motor Efficiency",       "%",       92,   None),
        ],
        "outputs": [
            ("adiabatic_power_kw",   "Adiabatic Shaft Power",    "kW",    True),
            ("isothermal_power_kw",  "Isothermal Shaft Power",   "kW",    False),
            ("motor_input_kw",       "Motor Input Power",        "kW",    False),
            ("discharge_temp_c",     "Discharge Temperature",    "°C",    False),
            ("compression_ratio",    "Compression Ratio (p₂/p₁)","—",     False),
            ("mass_flow_kg_s",       "Mass Flow (ṁ)",            "kg/s",  False),
            ("recommended_motor_kw", "Recommended IEC Motor",    "kW",    False),
        ],
        "formula": ("P_ad = ṁ·Cp·T₁·[(p₂/p₁)^((k-1)/k) − 1] / η_ad     ·     "
                    "P_iso = ṁ·R·T₁·ln(p₂/p₁) / η_iso"),
        "reference": {
            "kind": "gas_properties",
            "table": [
                {"material": "Air (dry)",       "c": 1.40, "extra": 29,
                 "apply": {"k_ratio": 1.4,  "gas_mw": 29}},
                {"material": "Nitrogen (N₂)",   "c": 1.40, "extra": 28,
                 "apply": {"k_ratio": 1.4,  "gas_mw": 28}},
                {"material": "Oxygen (O₂)",     "c": 1.40, "extra": 32,
                 "apply": {"k_ratio": 1.4,  "gas_mw": 32}},
                {"material": "Carbon dioxide",  "c": 1.28, "extra": 44,
                 "apply": {"k_ratio": 1.28, "gas_mw": 44}},
                {"material": "Methane (CH₄)",   "c": 1.31, "extra": 16,
                 "apply": {"k_ratio": 1.31, "gas_mw": 16}},
                {"material": "Ammonia (NH₃)",   "c": 1.31, "extra": 17,
                 "apply": {"k_ratio": 1.31, "gas_mw": 17}},
                {"material": "Hydrogen (H₂)",   "c": 1.41, "extra":  2,
                 "apply": {"k_ratio": 1.41, "gas_mw":  2}},
                {"material": "Natural gas (typ.)","c": 1.30,"extra": 18,
                 "apply": {"k_ratio": 1.30, "gas_mw": 18}},
            ],
        },
    },

    "screw-conveyor": {
        "id": 15, "name": "Screw Conveyor Sizing", "icon": "🌀",
        "category": "Conveyor", "fn": calc.m15_screw_conveyor,
        "inputs": [
            ("screw_dia_mm",     "Screw Diameter (D)",   "mm",     250, "length"),
            ("shaft_dia_mm",     "Shaft Diameter (d)",   "mm",      75, "length"),
            ("pitch_mm",         "Pitch (S)",            "mm",     250, "length"),
            ("rpm",              "Rotational Speed (N)", "rpm",     45, None),
            ("length_m",         "Length (L)",           "m",       10, "length"),
            ("incline_deg",      "Incline (θ)",          "°",        0, None),
            ("density_kg_m3",    "Bulk Density (ρ)",     "kg/m³", 1200, None),
            ("fill_factor_pct",  "Fill Factor (λ)",      "%",       30, None),
            ("material_factor",  "Material Factor (Fm)", "—",      2.0, None),
        ],
        "outputs": [
            ("capacity_m3h",        "Volumetric Capacity",   "m³/hr", True),
            ("capacity_tph",        "Mass Capacity",         "t/hr",  False),
            ("material_power_kw",   "Material Power",        "kW",    False),
            ("incline_power_kw",    "Incline Power",         "kW",    False),
            ("empty_power_kw",      "Empty Screw Power",     "kW",    False),
            ("shaft_power_kw",      "Total Shaft Power",     "kW",    False),
            ("motor_input_kw",      "Motor Input Power",     "kW",    False),
            ("recommended_motor_kw","Recommended IEC Motor", "kW",    False),
        ],
        "formula": ("Q = (π/4)·(D²−d²)·S·N·λ·60     ·     "
                    "P = (Q·L·Fm + Q·L·sinθ)/367 + (D·L·N)/100 000"),
        "reference": {
            "kind": "material_factor",
            "table": [
                {"material": "Barley / oats / wheat",     "c": 0.4},
                {"material": "Corn (shelled)",            "c": 0.4},
                {"material": "Flour, wheat",              "c": 0.6},
                {"material": "Lime, hydrated",            "c": 0.6},
                {"material": "Sawdust, dry",              "c": 0.7},
                {"material": "Sugar, powdered",           "c": 0.9},
                {"material": "Coal, powdered",            "c": 0.9},
                {"material": "Salt, common dry",          "c": 1.0},
                {"material": "Cement, Portland",          "c": 1.4},
                {"material": "Gypsum, calcined powder",   "c": 1.6},
                {"material": "Sand, dry",                 "c": 1.7},
                {"material": "Alumina, fine",             "c": 1.8},
                {"material": "Fly ash",                   "c": 2.0},
                {"material": "Bauxite, dry",              "c": 2.5},
            ],
        },
    },    
}


# ---------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login_view():
    if auth.current_user():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        u = auth.login(request.form.get("username", ""),
                       request.form.get("password", ""))
        if u:
            nxt = request.args.get("next") or url_for("dashboard")
            return redirect(nxt)
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout_view():
    auth.logout()
    return redirect(url_for("login_view"))


# ---------------------------------------------------------------
# Dashboard router  (owner vs user)
# ---------------------------------------------------------------
@app.route("/")
def dashboard():
    if (r := need_login()): return r
    u = auth.current_user()
    return owner_dashboard() if u["role"] == "owner" else user_dashboard()


def user_dashboard():
    u  = auth.current_user()
    db = auth.get_db()
    kpi = {
        "total":    _scalar(db, "SELECT COUNT(*) FROM calculations WHERE user_id=?", (u["id"],)),
        "pending":  _scalar(db, "SELECT COUNT(*) FROM calculations WHERE user_id=? AND status='pending'", (u["id"],)),
        "approved": _scalar(db, "SELECT COUNT(*) FROM calculations WHERE user_id=? AND status='approved'", (u["id"],)),
    }
    return render_template(
        "user_dashboard.html",
        kpi=kpi,
        modules=[m for m in calc.MODULE_REGISTRY],
        custom=[m for m in mb.list_modules(status="active", for_user_id=u["id"])],
    )


def owner_dashboard():
    db = auth.get_db()
    kpi = {
        "total":    _scalar(db, "SELECT COUNT(*) FROM calculations"),
        "users":    _scalar(db, "SELECT COUNT(*) FROM users"),
        "pending":  _scalar(db, "SELECT COUNT(*) FROM calculations WHERE status='pending'"),
        "approved": _scalar(db, "SELECT COUNT(*) FROM calculations WHERE status='approved'"),
    }
    # Trend: last 7 days
    trend_rows = db.execute("""
        SELECT substr(created_at,1,10) AS day, COUNT(*) AS n
          FROM calculations
         WHERE created_at >= ?
         GROUP BY day ORDER BY day ASC
    """, ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),)
    ).fetchall()
    by_module = db.execute("""
        SELECT module_name AS k, COUNT(*) AS n FROM calculations
         GROUP BY module_slug ORDER BY n DESC LIMIT 10
    """).fetchall()
    by_user = db.execute("""
        SELECT u.username AS k, COUNT(*) AS n
          FROM calculations c JOIN users u ON u.id = c.user_id
         GROUP BY c.user_id ORDER BY n DESC LIMIT 8
    """).fetchall()
    by_status = db.execute("""
        SELECT status AS k, COUNT(*) AS n FROM calculations
         GROUP BY status
    """).fetchall()
    recent = db.execute("""
        SELECT c.id, c.created_at, c.module_name, c.status, u.username AS user
          FROM calculations c JOIN users u ON u.id = c.user_id
         ORDER BY c.created_at DESC LIMIT 15
    """).fetchall()
    return render_template(
        "owner_dashboard.html",
        kpi=kpi,
        charts={
            "trend":     {"labels": [r["day"] for r in trend_rows],
                          "values": [r["n"]   for r in trend_rows]},
            "by_module": {"labels": [r["k"]   for r in by_module],
                          "values": [r["n"]   for r in by_module]},
            "by_user":   {"labels": [r["k"]   for r in by_user],
                          "values": [r["n"]   for r in by_user]},
            "by_status": {"labels": [r["k"]   for r in by_status],
                          "values": [r["n"]   for r in by_status]},
        },
        recent=recent,
    )


def _scalar(db, sql, params=()):
    row = db.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


# ---------------------------------------------------------------
# Hydraulic module dispatch  (routes 1–10 share this)
# ---------------------------------------------------------------
@app.route("/calc/<slug>", methods=["GET", "POST"])
def calc_view(slug):
    if (r := need_login()): return r
    cfg = HYDRAULIC_MODULES.get(slug)
    if not cfg:
        abort(404)

    values, result, error = {}, None, None

    # Prefill defaults or POSTed values
    for var, _, _, default, _ in cfg["inputs"]:
        values[var] = request.form.get(var, "") if request.method == "POST" \
                                                else str(default)

    if request.method == "POST":
        try:
            kwargs = {}
            for var, label, _, _, _ in cfg["inputs"]:
                if request.form.get(var, "").strip() == "":
                    raise ValueError(f"{label} is required.")
                kwargs[var] = safe_float(request.form.get(var))
            result = cfg["fn"](**kwargs)
            _store_last(slug, cfg["name"], cfg["icon"],
                        kwargs, result, cfg["formula"])
        except (ValueError, ZeroDivisionError) as e:
            error = str(e)

    return render_template(
        "module_calc.html",
        cfg=cfg, slug=slug, values=values,
        result=result, error=error,
    )


# ---------------------------------------------------------------
# Module 11 — Total Discharge / Pump Head  (custom UX)
# ---------------------------------------------------------------
@app.route("/calc/total-pump-head", methods=["GET", "POST"])
def total_pump_head():
    if (r := need_login()): return r

    defaults = dict(flow_m3h=212, pipe_id_mm=210.1, length_m=131,
                    c_factor=140, static_head_m=8, residual_head_m=2)
    values = {k: request.form.get(k, str(v)) for k, v in defaults.items()}
    result = None
    error  = None
    fittings_in: List[Dict[str, Any]] = []

    if request.method == "POST":
        # Rebuild fittings list from parallel arrays sent by the form
        keys = request.form.getlist("fitting_key")
        qtys = request.form.getlist("fitting_qty")
        for k, q in zip(keys, qtys):
            spec = next((f for f in calc.FITTINGS_K if f["key"] == k), None)
            try:
                q_int = int(q or 0)
            except ValueError:
                q_int = 0
            if spec and q_int > 0:
                fittings_in.append({"key": k, "name": spec["name"],
                                    "k": spec["k"], "qty": q_int})
        try:
            kwargs = {k: safe_float(values[k]) for k in defaults}
            result = calc.m11_total_pump_head(fittings=fittings_in, **kwargs)
            _store_last("total-pump-head", "Total Discharge / Pump Head", "💧",
                        {**kwargs, "fittings": fittings_in}, result,
                        "TDH = H_static + H_friction + Σ(K·V²/2g) + H_residual")
        except (ValueError, ZeroDivisionError) as e:
            error = str(e)

    return render_template(
        "total_pump_head.html",
        values=values, result=result, error=error,
        fittings_library=calc.FITTINGS_K,
        selected_fittings=fittings_in,
        hazen_c_table=calc.HAZEN_C_TABLE,
    )


# ---------------------------------------------------------------
# Custom (Owner-built) module dispatch
# ---------------------------------------------------------------
@app.route("/m/<slug>", methods=["GET", "POST"])
def custom_module_view(slug):
    if (r := need_login()): return r
    m = mb.get_module_by_slug(slug)
    if not m:
        abort(404)
    u = auth.current_user()
    if m["status"] == "disabled" and u["role"] != "owner":
        return render_template("maintenance.html", module=m)
    if m["status"] == "maintenance" and u["role"] != "owner":
        return render_template("maintenance.html", module=m)
    if m["assigned_users"] and u["id"] not in m["assigned_users"] \
       and u["role"] != "owner":
        abort(403)

    values = {i["var"]: (request.form.get(i["var"], "")
                         if request.method == "POST"
                         else str(i["default"]))
              for i in m["inputs"]}
    result = None
    error  = None
    if request.method == "POST":
        try:
            input_vals = {i["var"]: safe_float(request.form.get(i["var"], i["default"]))
                          for i in m["inputs"]}
            out = mb.evaluate(m["id"], input_vals)
            if out["errors"] and "_" in out["errors"]:
                error = out["errors"]["_"]
            else:
                result = out
                _store_last(f"custom:{m['slug']}", m["name"], m["icon"],
                            input_vals, out["results"],
                            "; ".join(f"{o['var']} = {o['formula']}"
                                      for o in m["outputs"]))
        except ValueError as e:
            error = str(e)

    return render_template(
        "dynamic_module.html",
        module=m, values=values, result=result, error=error,
        is_shared=False,
    )


# ---------------------------------------------------------------
# Public shared module  (view-only, no login)
# ---------------------------------------------------------------
@app.route("/share/<token>", methods=["GET", "POST"])
def shared_module_view(token):
    m = mb.resolve_share(token)
    if not m:
        return render_template("shared_module.html",
                               module=None, expired=True), 410
    values = {i["var"]: (request.form.get(i["var"], "")
                         if request.method == "POST"
                         else str(i["default"]))
              for i in m["inputs"]}
    result = None
    if request.method == "POST":
        input_vals = {i["var"]: safe_float(request.form.get(i["var"], i["default"]))
                      for i in m["inputs"]}
        result = mb.evaluate(m["id"], input_vals)
    return render_template(
        "shared_module.html",
        module=m, values=values, result=result,
        expired=False,
    )


# ---------------------------------------------------------------
# History
# ---------------------------------------------------------------
@app.route("/history")
def history():
    if (r := need_login()): return r
    u  = auth.current_user()
    db = auth.get_db()
    if u["role"] == "owner":
        rows = db.execute("""
            SELECT c.*, u.username AS user
              FROM calculations c JOIN users u ON u.id = c.user_id
             ORDER BY c.created_at DESC LIMIT 500
        """).fetchall()
    else:
        rows = db.execute("""
            SELECT c.*, u.username AS user
              FROM calculations c JOIN users u ON u.id = c.user_id
             WHERE c.user_id = ?
             ORDER BY c.created_at DESC LIMIT 500
        """, (u["id"],)).fetchall()
    return render_template("history.html", rows=rows)


@app.route("/history/<int:calc_id>/view")
def history_view(calc_id):
    if (r := need_login()): return r
    row = _get_calc(calc_id)
    if not row or not _can_touch_calc(row):
        abort(404)
    return jsonify({
        "id": row["id"], "module": row["module_name"],
        "icon": row["module_icon"], "status": row["status"],
        "created_at": row["created_at"], "formula": row["formula"],
        "inputs": row["inputs"], "results": row["results"],
        "review_comment": row["review_comment"],
    })


@app.route("/history/<int:calc_id>/pdf")
def history_pdf(calc_id):
    if (r := need_login()): return r
    row = _get_calc(calc_id)
    if not row or not _can_touch_calc(row):
        abort(404)
    inputs  = [{"label": k, "value": v} for k, v in row["inputs"].items()]
    results = [{"label": k, "value": v} for k, v in row["results"].items()]
    u   = auth.current_user()
    pdf, rid = pdf_generator.generate_report(
        module_name = row["module_name"],
        module_icon = row["module_icon"],
        inputs      = inputs,
        results     = results,
        formula     = row["formula"],
        user_name   = u["full_name"],
    )
    if not row["report_id"]:
        auth.get_db().execute(
            "UPDATE calculations SET report_id=? WHERE id=?", (rid, calc_id))
        auth.get_db().commit()
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=True,
                     download_name=f"{row['module_slug']}-{rid}.pdf")


@app.route("/history/<int:calc_id>/submit", methods=["POST"])
def history_submit(calc_id):
    if (r := need_login()): return r
    row = _get_calc(calc_id)
    if not row or not _can_touch_calc(row):
        abort(404)
    if row["status"] in ("pending", "approved"):
        flash("Already submitted.", "warn")
    else:
        auth.get_db().execute(
            "UPDATE calculations SET status='pending' WHERE id=?", (calc_id,))
        auth.get_db().commit()
        for owner in [x for x in auth.list_users_public() if x["role"] == "owner"]:
            push_notification(owner["id"], "New approval request",
                              f"{row['module_name']} submitted by "
                              f"{auth.current_user()['full_name']}",
                              "info", url_for("approvals"))
        flash("Submitted for approval.", "ok")
    return redirect(url_for("history"))


@app.route("/history/<int:calc_id>/cancel", methods=["POST"])
def history_cancel(calc_id):
    if (r := need_login()): return r
    row = _get_calc(calc_id)
    if not row or not _can_touch_calc(row):
        abort(404)
    if row["status"] == "pending":
        auth.get_db().execute(
            "UPDATE calculations SET status='draft' WHERE id=?", (calc_id,))
        auth.get_db().commit()
        flash("Approval cancelled.", "ok")
    return redirect(url_for("history"))


@app.route("/history/<int:calc_id>/delete", methods=["POST"])
def history_delete(calc_id):
    if (r := need_login()): return r
    row = _get_calc(calc_id)
    if not row or not _can_touch_calc(row):
        abort(404)
    auth.get_db().execute("DELETE FROM calculations WHERE id=?", (calc_id,))
    auth.get_db().commit()
    flash("Calculation deleted.", "ok")
    return redirect(url_for("history"))


@app.route("/history/delete-all", methods=["POST"])
def history_delete_all():
    if (r := need_login()): return r
    u  = auth.current_user()
    db = auth.get_db()
    if u["role"] == "owner":
        db.execute("DELETE FROM calculations")
    else:
        db.execute("DELETE FROM calculations WHERE user_id=?", (u["id"],))
    db.commit()
    flash("History cleared.", "ok")
    return redirect(url_for("history"))


# ---------------------------------------------------------------
# Compare  (2–4 calcs, Ctrl-click IDs come in via ?ids=1,2,3)
# ---------------------------------------------------------------
@app.route("/compare")
def compare():
    if (r := need_login()): return r
    raw = request.args.get("ids", "")
    try:
        ids = [int(x) for x in raw.split(",") if x.strip().isdigit()][:4]
    except ValueError:
        ids = []
    rows = []
    for cid in ids:
        row = _get_calc(cid)
        if row and _can_touch_calc(row):
            rows.append(row)
    # Also give a picker: recent calcs
    u  = auth.current_user()
    db = auth.get_db()
    pickable = db.execute(
        "SELECT id, module_name, status, created_at FROM calculations "
        "WHERE user_id=? OR ?=1 ORDER BY created_at DESC LIMIT 100",
        (u["id"], 1 if u["role"] == "owner" else 0)
    ).fetchall()
    return render_template("compare.html", rows=rows, pickable=pickable)


# ---------------------------------------------------------------
# Approvals  (owner only)
# ---------------------------------------------------------------
@app.route("/approvals")
def approvals():
    if (r := need_owner()): return r
    rows = auth.get_db().execute("""
        SELECT c.*, u.username AS user
          FROM calculations c JOIN users u ON u.id = c.user_id
         WHERE c.status='pending'
         ORDER BY c.created_at ASC
    """).fetchall()
    parsed = []
    for r in rows:
        d = dict(r)
        d["inputs"]  = json.loads(r["inputs_json"]  or "{}")
        d["results"] = json.loads(r["results_json"] or "{}")
        parsed.append(d)
    return render_template("approvals.html", rows=parsed)


@app.route("/approvals/<int:calc_id>/<action>", methods=["POST"])
def approvals_act(calc_id, action):
    if (r := need_owner()): return r
    if action not in ("approve", "reject", "delete"):
        abort(400)
    row = _get_calc(calc_id)
    if not row:
        abort(404)
    db = auth.get_db()
    u  = auth.current_user()
    if action == "delete":
        db.execute("DELETE FROM calculations WHERE id=?", (calc_id,))
    else:
        status  = "approved" if action == "approve" else "rejected"
        comment = request.form.get("comment", "").strip()
        db.execute(
            """UPDATE calculations
                 SET status=?, review_comment=?, reviewed_by=?, reviewed_at=?
               WHERE id=?""",
            (status, comment, u["id"], _now(), calc_id))
        push_notification(row["user_id"],
                          f"Calculation {status}",
                          f"{row['module_name']} — {comment or 'No comment.'}",
                          "ok" if status == "approved" else "warn",
                          url_for("history"))
    db.commit()
    flash(f"Action '{action}' complete.", "ok")
    return redirect(url_for("approvals"))


# ---------------------------------------------------------------
# User management  (owner only)
# ---------------------------------------------------------------
@app.route("/users", methods=["GET", "POST"])
def manage_users():
    if (r := need_owner()): return r
    if request.method == "POST":
        try:
            auth.add_user(
                username = request.form.get("username", ""),
                password = request.form.get("password", ""),
                role     = request.form.get("role", "user"),
                full_name= request.form.get("full_name", ""),
            )
            flash("User created.", "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("manage_users"))
    return render_template("manage_users.html",
                           users=auth.list_users_full())


@app.route("/users/<int:uid>/password", methods=["POST"])
def user_set_password(uid):
    if (r := need_owner()): return r
    try:
        auth.set_password(uid, request.form.get("password", ""))
        flash("Password updated.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("manage_users"))


@app.route("/users/<int:uid>/update", methods=["POST"])
def user_update(uid):
    if (r := need_owner()): return r
    try:
        auth.update_user(uid,
                         role=request.form.get("role"),
                         full_name=request.form.get("full_name"))
        flash("User updated.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("manage_users"))


@app.route("/users/<int:uid>/delete", methods=["POST"])
def user_delete(uid):
    if (r := need_owner()): return r
    try:
        auth.delete_user(uid, auth.current_user()["id"])
        flash("User deleted.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("manage_users"))


# ---------------------------------------------------------------
# Pump databank
# ---------------------------------------------------------------
@app.route("/pumps")
def pumps_list():
    if (r := need_login()): return r
    pumps = pdb.list_pumps(
        search=request.args.get("q") or None,
        sort  =request.args.get("sort") or "vendor",
        order =request.args.get("order") or "asc",
    )
    return render_template("pump_databank.html", pumps=pumps,
                           q=request.args.get("q", ""))


@app.route("/pumps/save", methods=["POST"])
def pumps_save():
    if (r := need_owner()): return r
    pid = request.form.get("id", "").strip()
    pdf_file = request.files.get("pdf")
    stream = pdf_file.stream if pdf_file and pdf_file.filename else None
    fname  = pdf_file.filename if pdf_file and pdf_file.filename else None
    try:
        if pid:
            pdb.update_pump(int(pid), request.form,
                            pdf_stream=stream, pdf_filename=fname,
                            remove_pdf=bool(request.form.get("remove_pdf")))
            flash("Pump updated.", "ok")
        else:
            pdb.create_pump(request.form,
                            pdf_stream=stream, pdf_filename=fname)
            flash("Pump added.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("pumps_list"))


@app.route("/pumps/<int:pid>/delete", methods=["POST"])
def pumps_delete(pid):
    if (r := need_owner()): return r
    pdb.delete_pump(pid)
    flash("Pump deleted.", "ok")
    return redirect(url_for("pumps_list"))


@app.route("/pumps/<int:pid>/pdf")
def pumps_pdf(pid):
    if (r := need_login()): return r
    info = pdb.pdf_path(pid)
    if not info:
        abort(404)
    path, name = info
    return send_file(path, mimetype="application/pdf",
                     as_attachment=True, download_name=name)


# ---------------------------------------------------------------
# Module Hub  (Installed / Marketplace / Build New)
# ---------------------------------------------------------------
@app.route("/hub")
def hub():
    if (r := need_owner()): return r
    return render_template("hub.html",
        installed = mb.list_modules(),
        hydraulic = list(HYDRAULIC_MODULES.values()),
    )


@app.route("/hub/clean-duplicates", methods=["POST"])
def hub_clean():
    if (r := need_owner()): return r
    n = mb.clean_duplicates()
    flash(f"Removed {n} duplicate module(s).", "ok")
    return redirect(url_for("hub"))


@app.route("/hub/new")
@app.route("/hub/<int:module_id>/edit")
def builder_edit(module_id=None):
    if (r := need_owner()): return r
    module = mb.get_module(module_id) if module_id else None
    versions = mb.list_versions(module_id) if module_id else []
    return render_template("builder_edit.html",
                           module=module,
                           versions=versions,
                           cheat_sheet=mb.CHEAT_SHEET,
                           users=auth.list_users_public())


@app.route("/hub/save", methods=["POST"])
def builder_save():
    if (r := need_owner()): return r
    try:
        payload = json.loads(request.form["payload"])
    except (KeyError, json.JSONDecodeError) as e:
        return jsonify({"ok": False, "error": f"Bad payload: {e}"}), 400
    mid = request.form.get("id")
    mid = int(mid) if mid and mid.isdigit() else None
    try:
        if not mid and mb.duplicate_check(payload.get("name", "")):
            return jsonify({"ok": False,
                            "error": "A module with this name already exists."}), 400
        m = mb.save_module(payload, auth.current_user()["id"], module_id=mid)
        return jsonify({"ok": True, "id": m["id"], "slug": m["slug"]})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/hub/<int:module_id>/uninstall", methods=["POST"])
def builder_uninstall(module_id):
    if (r := need_owner()): return r
    mb.delete_module(module_id)
    flash("Module uninstalled.", "ok")
    return redirect(url_for("hub"))


@app.route("/hub/<int:module_id>/status", methods=["POST"])
def builder_status(module_id):
    if (r := need_owner()): return r
    try:
        mb.set_status(module_id, request.form.get("status", "active"))
        flash("Status updated.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("hub"))


@app.route("/hub/<int:module_id>/rollback/<int:version>", methods=["POST"])
def builder_rollback(module_id, version):
    if (r := need_owner()): return r
    try:
        mb.rollback_to(module_id, version, auth.current_user()["id"])
        flash(f"Rolled back to v{version}.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("builder_edit", module_id=module_id))


@app.route("/hub/<int:module_id>/export")
def builder_export(module_id):
    if (r := need_owner()): return r
    data = mb.export_json(module_id)
    return send_file(io.BytesIO(json.dumps(data, indent=2).encode()),
                     mimetype="application/json", as_attachment=True,
                     download_name=f"module-{data['name']}.json")


@app.route("/hub/import", methods=["POST"])
def builder_import():
    if (r := need_owner()): return r
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("hub"))
    try:
        payload = json.loads(f.read().decode("utf-8"))
        m = mb.import_json(payload, auth.current_user()["id"])
        flash(f"Imported '{m['name']}'.", "ok")
    except (ValueError, json.JSONDecodeError) as e:
        flash(f"Import failed: {e}", "error")
    return redirect(url_for("hub"))


@app.route("/hub/<int:module_id>/share", methods=["POST"])
def builder_share(module_id):
    if (r := need_owner()): return r
    try:
        s = mb.create_share(
            module_id,
            ttl_hours=int(request.form.get("ttl_hours") or 0) or None,
            max_views=int(request.form.get("max_views") or 0) or None,
            actor=auth.current_user()["id"],
        )
        url = url_for("shared_module_view", token=s["token"], _external=True)
        return jsonify({"ok": True, "url": url, **s})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ---------------------------------------------------------------
# Batch import
# ---------------------------------------------------------------
@app.route("/batch", methods=["GET", "POST"])
def batch():
    if (r := need_login()): return r
    if request.method == "POST":
        slug = request.form.get("module_slug", "")
        f    = request.files.get("file")
        if not f or not f.filename:
            flash("Please choose an Excel/CSV file.", "error")
            return redirect(url_for("batch"))
        try:
            df = excel_import.read_batch(f.read(), f.filename)
            ok, bad = excel_import.run_batch(df, slug)
            out = excel_import.build_result_workbook(ok, bad, slug)
            return send_file(
                io.BytesIO(out),
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=f"batch-{slug}.xlsx")
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("batch"))

    # ✅ Strip non-serializable 'fn' key so Jinja's tojson can render it
    mods_safe = {
        slug: {k: v for k, v in spec.items() if k != "fn"}
        for slug, spec in excel_import.SUPPORTED_MODULES.items()
    }
    return render_template("batch.html", modules=mods_safe)


@app.route("/batch/template/<slug>")
def batch_template(slug):
    if (r := need_login()): return r
    try:
        data = excel_import.template_workbook(slug)
    except ValueError:
        abort(404)
    return send_file(
        io.BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"template-{slug}.xlsx")


# ---------------------------------------------------------------
# Excel exports
# ---------------------------------------------------------------
@app.route("/export/log.xlsx")
def export_log():
    if (r := need_owner()): return r
    rows = auth.get_db().execute("""
        SELECT c.id, c.created_at, u.username AS user, u.role,
               c.module_name AS module, 'run' AS action, c.status,
               c.report_id,
               c.inputs_json AS inputs, c.results_json AS results,
               c.review_comment AS notes
          FROM calculations c JOIN users u ON u.id = c.user_id
         ORDER BY c.created_at DESC
    """).fetchall()
    data = excel_export.export_activity_log(
        [{k: r[k] for k in r.keys()} for r in rows])
    return send_file(io.BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name="calcvault-activity.xlsx")


@app.route("/export/history.xlsx")
def export_history():
    if (r := need_login()): return r
    u = auth.current_user()
    rows = auth.get_db().execute("""
        SELECT id, created_at, module_name AS module, status, report_id,
               inputs_json AS inputs, results_json AS results
          FROM calculations WHERE user_id=? ORDER BY created_at DESC
    """, (u["id"],)).fetchall()
    data = excel_export.export_history(
        [{k: r[k] for k in r.keys()} for r in rows])
    return send_file(io.BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name="my-calculations.xlsx")


@app.route("/export/pumps.xlsx")
def export_pumps():
    if (r := need_owner()): return r
    data = excel_export.export_pump_databank(pdb.list_pumps())
    return send_file(io.BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name="pump-databank.xlsx")


# ---------------------------------------------------------------
# JSON API for the live UI
# ---------------------------------------------------------------
@app.route("/api/heartbeat")
def api_heartbeat():
    u = auth.current_user()
    if u:
        auth.heartbeat(u["id"])
        return jsonify({"ok": True,
                        "online": auth.online_count(2),
                        "server_time": _now()})
    return jsonify({"ok": False}), 401


@app.route("/api/notifications")
def api_notifications():
    if (r := need_login()): return r
    u = auth.current_user()
    rows = auth.get_db().execute(
        """SELECT id, kind, title, message, link, is_read, created_at
             FROM notifications
            WHERE user_id=?
            ORDER BY created_at DESC LIMIT 20""",
        (u["id"],)).fetchall()
    unread = sum(1 for r in rows if not r["is_read"])
    return jsonify({"items": [dict(r) for r in rows], "unread": unread})


@app.route("/api/notifications/mark-read", methods=["POST"])
def api_notif_mark():
    if (r := need_login()): return r
    u = auth.current_user()
    auth.get_db().execute(
        "UPDATE notifications SET is_read=1 WHERE user_id=?", (u["id"],))
    auth.get_db().commit()
    return jsonify({"ok": True})


@app.route("/api/pump-suggest")
def api_pump_suggest():
    if (r := need_login()): return r
    return jsonify(pdb.suggest_for_duty(
        safe_float(request.args.get("flow")),
        safe_float(request.args.get("head")),
    ))


@app.route("/api/module-preview", methods=["POST"])
def api_module_preview():
    """Live-preview endpoint for the Module Builder test panel."""
    if (r := need_owner()): return r
    try:
        payload = request.get_json(force=True) or {}
        # Evaluate without persisting: build a transient module dict
        inputs  = payload.get("inputs",  [])
        outputs = payload.get("outputs", [])
        # We reuse the same validation + eval path by staging in memory
        # via a temporary save-then-eval would be wasteful; instead:
        from module_builder import (_validate_inputs, _validate_outputs,
                                    _topo_sort, _normalise_formula,
                                    _get_evaluator, FUNCTION_NAMES, CONSTANTS)
        ins  = _validate_inputs(inputs)
        outs = _validate_outputs(outputs, [i["var"] for i in ins])
        order = _topo_sort(outs, [i["var"] for i in ins])
        names: Dict[str, Any] = dict(CONSTANTS)
        for spec in ins:
            names[spec["var"]] = safe_float(
                payload.get("values", {}).get(spec["var"], spec["default"]),
                spec["default"])
        ev = _get_evaluator("_shared_"); ev.functions = FUNCTION_NAMES
        results, errors = {}, {}
        out_map = {o["var"]: o for o in outs}
        for v in order:
            spec = out_map[v]
            ev.names = names
            try:
                val = ev.eval(_normalise_formula(spec["formula"]))
                val = round(float(val), spec.get("decimals", 3))
                results[v] = val
                names[v]   = val
            except Exception as e:  # noqa: BLE001
                errors[v]  = str(e); results[v] = None
        return jsonify({"ok": not errors, "results": results, "errors": errors})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ---------------------------------------------------------------
# Shutdown  (owner)
# ---------------------------------------------------------------
@app.route("/shutdown", methods=["POST"])
def shutdown():
    if (r := need_owner()): return r
    def _die():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
    return "Server shutting down…", 200


# ---------------------------------------------------------------
# Error pages
# ---------------------------------------------------------------
@app.errorhandler(403)
def _403(_e): return render_template("maintenance.html",
    module={"name": "Access denied",
            "description": "You are not authorised to view this page."}), 403

@app.errorhandler(404)
def _404(_e): return render_template("maintenance.html",
    module={"name": "Not found",
            "description": "The requested page does not exist."}), 404


# ===============================================================
# Startup banner + launcher
# ===============================================================
def _print_banner(port: int) -> None:
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        host_ip = "?"
    bar = "═" * 62
    print(f"\n{bar}")
    print(f"  🚀  CalcVault — Ramboll Edition")
    print(f"{bar}")
    print(f"  Local :   http://127.0.0.1:{port}")
    print(f"  LAN   :   http://{host_ip}:{port}")
    print(f"  Host  :   http://{socket.gethostname()}:{port}")
    print(f"  DB    :   {DB_PATH}")
    print(f"{bar}\n")


def _open_browser_soon(port: int) -> None:
    def _go():
        time.sleep(1.0)
        try: webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception: pass
    threading.Thread(target=_go, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("CV_PORT", "5000"))
    _print_banner(port)
    if "--no-browser" not in sys.argv:
        _open_browser_soon(port)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        # Fallback dev server
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)