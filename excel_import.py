"""
excel_import.py — CalcVault (Ramboll Edition)
=============================================
Batch calculation runner: upload one .xlsx with rows of inputs,
pick a module, get back a results workbook.

Design:
    • Pandas reads the input file (handles .xlsx and .csv, coerces
      numerics for us, tolerates messy user files).
    • Column names are matched case-insensitively AND with alias
      support (a "Flow" column and a "Q" column both map to `flow`).
    • Each row is routed through calculations.py's pure functions.
    • Failures do NOT stop the batch — bad rows are marked with the
      error message in an "_error_" column so the engineer can fix
      only what's broken and re-run.

Public API:
    read_batch(file_bytes, filename)  -> pandas.DataFrame
    run_batch(df, module_slug)        -> tuple[list[dict], list[dict]]
                                          (results_rows, error_rows)
    build_result_workbook(results_rows,
                          error_rows,
                          module_slug) -> bytes    # ready for send_file
    template_workbook(module_slug)    -> bytes    # blank input template

    SUPPORTED_MODULES  -> {slug: {label, inputs:[{key,aliases,unit}]}}
"""

from __future__ import annotations
import io
from typing import List, Dict, Any, Tuple

import pandas as pd

import calculations as calc
from excel_export import _write_sheet, _stamp, _to_bytes   # style reuse
from openpyxl import Workbook


# ===============================================================
# Input specifications  (single source of truth for batch)
# ===============================================================
# Each entry maps a module slug to:
#   fn:      the calculations.py function
#   label:   friendly module name
#   inputs:  ordered list of parameter specs
#     key:     the kwarg name expected by fn
#     aliases: acceptable column names in the user's Excel
#     unit:    display hint (also written into the template)
# ---------------------------------------------------------------
SUPPORTED_MODULES: Dict[str, Dict[str, Any]] = {
    "pipe-diameter": {
        "fn": calc.m1_pipe_diameter, "label": "Pipe Diameter Sizing",
        "inputs": [
            {"key": "flow_m3h",    "unit": "m³/hr",
             "aliases": ["flow_m3h", "flow", "q"]},
            {"key": "velocity_ms", "unit": "m/s",
             "aliases": ["velocity_ms", "velocity", "v"]},
        ],
    },
    "pipe-head-loss": {
        "fn": calc.m2_pipe_head_loss, "label": "Pipe Head Loss (Hazen-Williams)",
        "inputs": [
            {"key": "flow_m3h",   "unit": "m³/hr",
             "aliases": ["flow_m3h", "flow", "q"]},
            {"key": "c_factor",   "unit": "—",
             "aliases": ["c_factor", "c", "hazen_c"]},
            {"key": "pipe_id_mm", "unit": "mm",
             "aliases": ["pipe_id_mm", "pipe_id", "id_mm", "id", "diameter_mm"]},
            {"key": "length_m",   "unit": "m",
             "aliases": ["length_m", "length", "l"]},
        ],
    },
    "flow-through-pipe": {
        "fn": calc.m3_flow_through_pipe, "label": "Flow Through Pipe",
        "inputs": [
            {"key": "dia_mm",      "unit": "mm",
             "aliases": ["dia_mm", "diameter_mm", "d_mm", "d"]},
            {"key": "velocity_ms", "unit": "m/s",
             "aliases": ["velocity_ms", "velocity", "v"]},
        ],
    },
    "channel-sizing": {
        "fn": calc.m4_channel_sizing, "label": "Channel Sizing",
        "inputs": [
            {"key": "flow_m3h",         "unit": "m³/hr",
             "aliases": ["flow_m3h", "flow", "q"]},
            {"key": "velocity_ms",      "unit": "m/s",
             "aliases": ["velocity_ms", "velocity", "v"]},
            {"key": "liquid_depth_mm",  "unit": "mm",
             "aliases": ["liquid_depth_mm", "liquid_depth", "ld_mm", "depth"]},
        ],
    },
    "channel-head-loss": {
        "fn": calc.m5_channel_head_loss, "label": "Channel Head Loss (Manning)",
        "inputs": [
            {"key": "flow_m3h",       "unit": "m³/hr",
             "aliases": ["flow_m3h", "flow", "q"]},
            {"key": "width_m",        "unit": "m",
             "aliases": ["width_m", "width", "w"]},
            {"key": "liquid_depth_m", "unit": "m",
             "aliases": ["liquid_depth_m", "liquid_depth", "ld", "depth"]},
            {"key": "length_m",       "unit": "m",
             "aliases": ["length_m", "length", "l"]},
            {"key": "n_kutter",       "unit": "—",
             "aliases": ["n_kutter", "n", "manning_n"]},
        ],
    },
    "tank-volume": {
        "fn": calc.m6_tank_volume, "label": "Circular Tank Volume",
        "inputs": [
            {"key": "diameter_m",      "unit": "m",
             "aliases": ["diameter_m", "diameter", "d"]},
            {"key": "total_height_m",  "unit": "m",
             "aliases": ["total_height_m", "height", "h"]},
            {"key": "freeboard_m",     "unit": "m",
             "aliases": ["freeboard_m", "freeboard", "fb"]},
        ],
    },
    "liquid-height": {
        "fn": calc.m7_liquid_height, "label": "Liquid Height in Tank",
        "inputs": [
            {"key": "volume_m3",       "unit": "m³",
             "aliases": ["volume_m3", "volume", "v"]},
            {"key": "diameter_m",      "unit": "m",
             "aliases": ["diameter_m", "diameter", "d"]},
            {"key": "total_height_m",  "unit": "m",
             "aliases": ["total_height_m", "height", "h"]},
            {"key": "freeboard_m",     "unit": "m",
             "aliases": ["freeboard_m", "freeboard", "fb"]},
        ],
    },
    "tank-diameter": {
        "fn": calc.m8_tank_diameter, "label": "Tank Diameter Sizing",
        "inputs": [
            {"key": "volume_m3",      "unit": "m³",
             "aliases": ["volume_m3", "volume", "v"]},
            {"key": "total_height_m", "unit": "m",
             "aliases": ["total_height_m", "height", "h"]},
        ],
    },
    "bell-mouth": {
        "fn": calc.m9_bellmouth, "label": "Bell-Mouth Entry Head Loss",
        "inputs": [
            {"key": "flow_m3h",        "unit": "m³/hr",
             "aliases": ["flow_m3h", "flow", "q"]},
            {"key": "bell_diameter_m", "unit": "m",
             "aliases": ["bell_diameter_m", "bell_diameter", "diameter", "d"]},
        ],
    },
    "weir": {
        "fn": calc.m10_weir, "label": "Rectangular Weir Head Loss",
        "inputs": [
            {"key": "flow_m3h",     "unit": "m³/hr",
             "aliases": ["flow_m3h", "flow", "q"]},
            {"key": "weir_length_m","unit": "m",
             "aliases": ["weir_length_m", "weir_length", "length", "l"]},
        ],
    },
    "pump-power": {
        "fn": calc.m12_pump_power, "label": "Pump Power Calculator",
        "inputs": [
            {"key": "flow_m3h",      "unit": "m³/hr",
             "aliases": ["flow_m3h", "flow", "q"]},
            {"key": "head_m",        "unit": "m",
             "aliases": ["head_m", "head", "h", "tdh"]},
            {"key": "pump_eff_pct",  "unit": "%",
             "aliases": ["pump_eff_pct", "pump_eff", "pump_efficiency", "eta_p"]},
            {"key": "motor_eff_pct", "unit": "%",
             "aliases": ["motor_eff_pct", "motor_eff", "motor_efficiency", "eta_m"]},
            {"key": "density_kg_m3", "unit": "kg/m³",
             "aliases": ["density_kg_m3", "density", "rho"]},
        ],
    },
    "pump-affinity": {
        "fn": calc.m13_pump_affinity, "label": "Pump Affinity Laws",
        "inputs": [
            {"key": "flow_m3h_1",    "unit": "m³/hr",
             "aliases": ["flow_m3h_1", "q1", "flow_1"]},
            {"key": "head_m_1",      "unit": "m",
             "aliases": ["head_m_1", "h1", "head_1"]},
            {"key": "power_kw_1",    "unit": "kW",
             "aliases": ["power_kw_1", "p1", "power_1"]},
            {"key": "speed_rpm_1",   "unit": "rpm",
             "aliases": ["speed_rpm_1", "n1", "rpm1"]},
            {"key": "speed_rpm_2",   "unit": "rpm",
             "aliases": ["speed_rpm_2", "n2", "rpm2"]},
            {"key": "impeller_mm_1", "unit": "mm",
             "aliases": ["impeller_mm_1", "d1"]},
            {"key": "impeller_mm_2", "unit": "mm",
             "aliases": ["impeller_mm_2", "d2"]},
        ],
    },

    "blower-power": {
        "fn": calc.m14_blower_power, "label": "Air Blower Power",
        "inputs": [
            {"key": "flow_nm3h",         "unit": "Nm³/hr",
             "aliases": ["flow_nm3h", "flow", "q", "q_normal"]},
            {"key": "p_suction_kpa_a",   "unit": "kPa_a",
             "aliases": ["p_suction_kpa_a", "p1", "p_suction"]},
            {"key": "p_discharge_kpa_a", "unit": "kPa_a",
             "aliases": ["p_discharge_kpa_a", "p2", "p_discharge"]},
            {"key": "temp_suction_c",    "unit": "°C",
             "aliases": ["temp_suction_c", "t1", "temp"]},
            {"key": "k_ratio",           "unit": "—",
             "aliases": ["k_ratio", "k"]},
            {"key": "gas_mw",            "unit": "kg/kmol",
             "aliases": ["gas_mw", "mw"]},
            {"key": "eta_adiabatic_pct", "unit": "%",
             "aliases": ["eta_adiabatic_pct", "eta_ad", "adiabatic_eff"]},
            {"key": "eta_motor_pct",     "unit": "%",
             "aliases": ["eta_motor_pct", "eta_m", "motor_eff"]},
        ],
    },

    "screw-conveyor": {
        "fn": calc.m15_screw_conveyor, "label": "Screw Conveyor Sizing",
        "inputs": [
            {"key": "screw_dia_mm",    "unit": "mm",
             "aliases": ["screw_dia_mm", "d", "diameter"]},
            {"key": "shaft_dia_mm",    "unit": "mm",
             "aliases": ["shaft_dia_mm", "shaft_d"]},
            {"key": "pitch_mm",        "unit": "mm",
             "aliases": ["pitch_mm", "pitch", "s"]},
            {"key": "rpm",             "unit": "rpm",
             "aliases": ["rpm", "n", "speed"]},
            {"key": "length_m",        "unit": "m",
             "aliases": ["length_m", "length", "l"]},
            {"key": "incline_deg",     "unit": "°",
             "aliases": ["incline_deg", "incline", "theta"]},
            {"key": "density_kg_m3",   "unit": "kg/m³",
             "aliases": ["density_kg_m3", "density", "rho"]},
            {"key": "fill_factor_pct", "unit": "%",
             "aliases": ["fill_factor_pct", "fill", "lambda"]},
            {"key": "material_factor", "unit": "—",
             "aliases": ["material_factor", "fm"]},
        ],
    },
}


# ===============================================================
# Reader
# ===============================================================
def read_batch(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Read .xlsx or .csv into a DataFrame. Raises ValueError on bad file."""
    if not file_bytes:
        raise ValueError("Empty file.")
    name = (filename or "").lower()
    buf = io.BytesIO(file_bytes)
    try:
        if name.endswith(".csv"):
            df = pd.read_csv(buf)
        elif name.endswith((".xlsx", ".xlsm")):
            df = pd.read_excel(buf, engine="openpyxl")
        else:
            raise ValueError("Only .xlsx and .csv files are supported.")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Could not read file: {e}") from e

    if df.empty:
        raise ValueError("File contains no rows.")

    # Normalise column names (strip + lower + underscore-collapsed)
    df.columns = [_norm_col(c) for c in df.columns]
    return df


def _norm_col(col: Any) -> str:
    return (str(col)
            .strip()
            .lower()
            .replace(" ", "_")
            .replace("-", "_")
            .replace("/", "_")
            .replace("__", "_"))


# ===============================================================
# Batch runner
# ===============================================================
def _resolve_column(df_cols: List[str], aliases: List[str]) -> str | None:
    lookup = {c: c for c in df_cols}
    for a in aliases:
        n = _norm_col(a)
        if n in lookup:
            return lookup[n]
    return None


def run_batch(df: pd.DataFrame, module_slug: str
              ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Execute the chosen module across every row.
    Returns (results_rows, error_rows).
    """
    if module_slug not in SUPPORTED_MODULES:
        raise ValueError(f"Unknown module: {module_slug!r}")
    spec = SUPPORTED_MODULES[module_slug]
    fn   = spec["fn"]

    # Resolve which sheet column feeds which kwarg (once, not per row)
    col_map: Dict[str, str | None] = {
        inp["key"]: _resolve_column(list(df.columns), inp["aliases"])
        for inp in spec["inputs"]
    }
    missing = [inp["key"] for inp, col in
               zip(spec["inputs"], col_map.values()) if col is None]
    if missing:
        raise ValueError(
            f"Missing required column(s) for '{spec['label']}': "
            + ", ".join(missing)
        )

    results, errors = [], []
    for i, row in df.iterrows():
        row_no = int(i) + 2   # +2 accounts for header row + 1-index
        try:
            kwargs = {}
            for key, col in col_map.items():
                v = row[col]
                if pd.isna(v):
                    raise ValueError(f"Missing value in column '{col}'")
                kwargs[key] = float(v)
            out = fn(**kwargs)
            results.append({"_row_": row_no, **kwargs, **out})
        except Exception as e:  # noqa: BLE001 — collect ALL row errors
            errors.append({"_row_": row_no,
                           "_error_": str(e),
                           **{c: row.get(c) for c in df.columns}})
    return results, errors


# ===============================================================
# Result workbook  (reuses excel_export styling)
# ===============================================================
def build_result_workbook(results_rows: List[Dict[str, Any]],
                          error_rows:   List[Dict[str, Any]],
                          module_slug:  str) -> bytes:
    label = SUPPORTED_MODULES[module_slug]["label"]
    wb = Workbook()
    # First sheet is auto-created — overwrite via _write_sheet
    _write_sheet(wb, "Results", results_rows,
                 preferred_order=["_row_"])
    if error_rows:
        _write_sheet(wb, "Errors", error_rows,
                     preferred_order=["_row_", "_error_"])
    _stamp(wb, f"CalcVault Batch — {label}")
    return _to_bytes(wb)


# ===============================================================
# Blank input template
# ===============================================================
def template_workbook(module_slug: str) -> bytes:
    if module_slug not in SUPPORTED_MODULES:
        raise ValueError(f"Unknown module: {module_slug!r}")
    spec = SUPPORTED_MODULES[module_slug]

    # Build ONE empty row so users can see the units in row 2
    demo = {inp["key"]: "" for inp in spec["inputs"]}
    template_rows = [demo, {inp["key"]: f"e.g. value in {inp['unit']}"
                            for inp in spec["inputs"]}]

    wb = Workbook()
    _write_sheet(wb, "Inputs", template_rows,
                 preferred_order=[i["key"] for i in spec["inputs"]])
    _stamp(wb, f"CalcVault Batch Template — {spec['label']}")
    return _to_bytes(wb)


# ===============================================================
# Smoke test
# ===============================================================
if __name__ == "__main__":
    # Build a fake in-memory input file for pipe-head-loss
    buf = io.BytesIO()
    pd.DataFrame([
        {"Flow": 212, "C": 140, "ID_mm": 210.1, "L":  131},
        {"Flow": 100, "C": 150, "ID_mm": 100.0, "L":   50},
        {"Flow": "bad", "C": 140, "ID_mm": 100, "L": 50},   # should error
    ]).to_excel(buf, index=False)
    df   = read_batch(buf.getvalue(), "test.xlsx")
    ok, bad = run_batch(df, "pipe-head-loss")
    out = build_result_workbook(ok, bad, "pipe-head-loss")
    with open("_test_batch_result.xlsx", "wb") as f:
        f.write(out)
    print(f"✅ batch: {len(ok)} ok, {len(bad)} errors")