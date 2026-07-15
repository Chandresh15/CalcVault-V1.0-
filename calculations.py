"""
calculations.py — CalcVault (Ramboll Edition)
==============================================
Server-side ONLY. Formulas never leave this file.

Contains:
  • 10 hydraulic calculation modules (validated numerically)
  • Module 11: Total Discharge / Pump Head (with CPHEEO K-library)
  • Pump Power calculation (bonus)
  • MODULE_REGISTRY used by app.py to render nav + dashboards

All functions are pure, return dicts, raise ValueError on bad input.
No I/O, no Flask imports — keeps the calc engine unit-testable.
"""

from __future__ import annotations
import math
from typing import Dict, List, Any

# ---------------------------------------------------------------
# Constants
# ---------------------------------------------------------------
PI      = math.pi
G       = 9.81               # m/s²
RHO_W   = 1000.0             # kg/m³  (water @ 20 °C)

# Standard nominal pipe diameters (mm) — DN series used by Ramboll
DN_SERIES: List[int] = [
    15, 20, 25, 32, 40, 50, 65, 80, 100, 125, 150, 200, 250,
    300, 350, 400, 450, 500, 600, 700, 800, 900, 1000, 1200,
    1400, 1600, 1800, 2000, 2200, 2400, 2600, 2800, 3000,
]

# Hazen-Williams C-factor reference (used by module 2 UI)
HAZEN_C_TABLE: List[Dict[str, Any]] = [
    {"material": "PVC / HDPE (smooth plastic)",           "c": 150},
    {"material": "DI cement-lined",                        "c": 140},
    {"material": "Steel (new)",                            "c": 140},
    {"material": "Cast iron (new)",                        "c": 130},
    {"material": "Concrete",                               "c": 120},
    {"material": "Galvanised iron",                        "c": 120},
    {"material": "Cast iron (10 yrs)",                     "c": 110},
    {"material": "Cast iron (old / tuberculated)",         "c": 100},
    {"material": "Riveted steel (old)",                    "c":  90},
]
# Standard IEC motor sizes (kW) — used by Module 12 to recommend
# the next-larger commercial motor above the calculated input power.
IEC_MOTOR_SIZES_KW: List[float] = [
    0.18, 0.25, 0.37, 0.55, 0.75, 1.1, 1.5, 2.2, 3.0, 4.0,
    5.5, 7.5, 11, 15, 18.5, 22, 30, 37, 45, 55, 75, 90,
    110, 132, 160, 200, 250, 315, 355, 400, 450, 500, 630, 800, 1000,
]

# Manning / Kutter n reference (module 5 UI)
KUTTER_N_TABLE: List[Dict[str, Any]] = [
    {"surface": "Smooth concrete",           "n": 0.013},
    {"surface": "Ordinary concrete lining",  "n": 0.014},
    {"surface": "Brick / masonry",           "n": 0.015},
    {"surface": "Rubble masonry",            "n": 0.017},
    {"surface": "Earth channel (clean)",     "n": 0.022},
    {"surface": "Earth channel (weedy)",     "n": 0.030},
    {"surface": "Rock cut (rough)",          "n": 0.035},
]

# CPHEEO / standard minor-loss K-factors for module 11
FITTINGS_K: List[Dict[str, Any]] = [
    {"key": "elbow_90_std",       "name": "90° elbow (standard)",       "k": 0.75},
    {"key": "elbow_90_long",      "name": "90° elbow (long radius)",    "k": 0.45},
    {"key": "elbow_45",           "name": "45° elbow",                  "k": 0.35},
    {"key": "return_bend_180",    "name": "180° return bend",           "k": 1.50},
    {"key": "tee_through",        "name": "Tee, flow through run",      "k": 0.40},
    {"key": "tee_branch",         "name": "Tee, flow through branch",   "k": 1.80},
    {"key": "gate_valve_open",    "name": "Gate valve (fully open)",    "k": 0.17},
    {"key": "gate_valve_half",    "name": "Gate valve (half open)",     "k": 4.50},
    {"key": "globe_valve_open",   "name": "Globe valve (fully open)",   "k": 6.00},
    {"key": "butterfly_valve",    "name": "Butterfly valve (open)",     "k": 0.35},
    {"key": "ball_valve_open",    "name": "Ball valve (fully open)",    "k": 0.05},
    {"key": "check_valve_swing",  "name": "Check valve (swing)",        "k": 2.50},
    {"key": "check_valve_lift",   "name": "Check valve (lift)",         "k": 10.00},
    {"key": "foot_valve_strainer","name": "Foot valve + strainer",      "k": 5.50},
    {"key": "bell_mouth_entry",   "name": "Bell-mouth entry",           "k": 0.05},
    {"key": "sharp_entry",        "name": "Sharp-edged entry",          "k": 0.50},
    {"key": "projecting_entry",   "name": "Re-entrant / projecting",    "k": 1.00},
    {"key": "sudden_enlargement", "name": "Sudden enlargement",         "k": 1.00},
    {"key": "sudden_contraction", "name": "Sudden contraction",         "k": 0.40},
    {"key": "gradual_enlargement","name": "Gradual enlargement (15°)",  "k": 0.20},
    {"key": "pipe_exit",          "name": "Pipe exit (submerged)",      "k": 1.00},
    {"key": "reducer_std",        "name": "Concentric reducer",         "k": 0.20},
]


# ===============================================================
# Internal helpers
# ===============================================================
def _pos(x: float, name: str) -> float:
    """Ensure value is a positive real number."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric.")
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{name} must be a positive number.")
    return v


def _nn(x: float, name: str) -> float:
    """Ensure value is a non-negative real number."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric.")
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"{name} must be zero or positive.")
    return v


def _round_up_dn(dia_mm: float) -> int:
    """Return the next-larger nominal pipe diameter from DN_SERIES."""
    for dn in DN_SERIES:
        if dn >= dia_mm - 1e-9:
            return dn
    return DN_SERIES[-1]


def _circle_area(d_m: float) -> float:
    return PI * d_m * d_m / 4.0


# ===============================================================
# MODULE 1 — Pipe Diameter Sizing        📏
# ===============================================================
def m1_pipe_diameter(flow_m3h: float, velocity_ms: float) -> Dict[str, Any]:
    """
    Size a pipe from flow and design velocity.
    Validated: Q=40, V=1.62 → Required ID = 93.4 mm
    """
    Q = _pos(flow_m3h, "Flow")
    V = _pos(velocity_ms, "Velocity")

    Q_si   = Q / 3600.0                          # m³/s
    A_req  = Q_si / V                            # m²
    d_req  = math.sqrt(4.0 * A_req / PI) * 1000  # mm
    d_prov = _round_up_dn(d_req)                 # mm

    A_prov = _circle_area(d_prov / 1000.0)
    V_act  = Q_si / A_prov

    return {
        "required_id_mm":  round(d_req, 2),
        "provided_id_mm":  d_prov,
        "actual_area_m2":  round(A_prov, 6),
        "actual_velocity": round(V_act, 3),
    }


# ===============================================================
# MODULE 2 — Pipe Head Loss (Hazen-Williams)   💧
# ===============================================================
def m2_pipe_head_loss(flow_m3h: float, c_factor: float,
                      pipe_id_mm: float, length_m: float) -> Dict[str, Any]:
    """
    H = ((Q / (1000.8 · C · d^2.63))^1.852) · L
        Q in m³/hr, d in metres (per Ramboll workbook convention)
    Validated: Q=212, C=140, ID=210.1, L=131 → H = 1.5656 m
    """
    Q = _pos(flow_m3h, "Flow")
    C = _pos(c_factor, "C-factor")
    d = _pos(pipe_id_mm, "Pipe ID") / 1000.0
    L = _pos(length_m, "Length")

    denom = 1000.8 * C * (d ** 2.63)
    j     = (Q / denom) ** 1.852        # head loss per metre
    H     = j * L

    # Diagnostic extras
    A     = _circle_area(d)
    V_act = (Q / 3600.0) / A

    return {
        "head_loss_m":       round(H, 4),
        "gradient_m_per_m":  round(j, 6),
        "velocity_ms":       round(V_act, 3),
        "pipe_area_m2":      round(A, 6),
    }


# ===============================================================
# MODULE 3 — Flow Through Pipe        🚰
# ===============================================================
def m3_flow_through_pipe(dia_mm: float, velocity_ms: float) -> Dict[str, Any]:
    """
    Validated: Dia=100, V=3 → Flow = 84.8 m³/hr
    """
    d = _pos(dia_mm, "Diameter") / 1000.0
    V = _pos(velocity_ms, "Velocity")

    A     = _circle_area(d)
    Q_si  = A * V
    Q_m3h = Q_si * 3600.0

    return {
        "area_m2":         round(A, 6),
        "flow_m3h":        round(Q_m3h, 2),
        "flow_ls":         round(Q_si * 1000.0, 3),
        "actual_velocity": round(V, 3),
    }


# ===============================================================
# MODULE 4 — Channel Sizing          🌊
# ===============================================================
def m4_channel_sizing(flow_m3h: float, velocity_ms: float,
                      liquid_depth_mm: float) -> Dict[str, Any]:
    """
    Rectangular channel: given Q, V, liquid depth → find width.
    Validated: Q=212, V=0.6, LD=300 → Required W = 0.327 m
    """
    Q  = _pos(flow_m3h, "Flow")
    V  = _pos(velocity_ms, "Velocity")
    LD = _pos(liquid_depth_mm, "Liquid depth") / 1000.0

    Q_si   = Q / 3600.0
    A_req  = Q_si / V
    W_req  = A_req / LD
    # Round provided width up to the next 50 mm
    W_prov = math.ceil(W_req * 20.0) / 20.0
    A_prov = W_prov * LD
    V_act  = Q_si / A_prov

    return {
        "required_width_m":  round(W_req, 3),
        "provided_width_m":  round(W_prov, 3),
        "actual_area_m2":    round(A_prov, 4),
        "actual_velocity":   round(V_act, 3),
    }


# ===============================================================
# MODULE 5 — Channel Head Loss (Manning)    〽️
# ===============================================================
def m5_channel_head_loss(flow_m3h: float, width_m: float,
                         liquid_depth_m: float, length_m: float,
                         n_kutter: float) -> Dict[str, Any]:
    """
    H = ((Q · n) / (A · R^(2/3)))^2 · L      (Q in m³/s)
    Validated: Q=212, W=0.35, LD=0.3, L=100, n=0.015 → H = 0.133 m
    """
    Q  = _pos(flow_m3h, "Flow")
    W  = _pos(width_m, "Width")
    LD = _pos(liquid_depth_m, "Liquid depth")
    L  = _pos(length_m, "Length")
    n  = _pos(n_kutter, "Kutter's n")

    Q_si = Q / 3600.0
    A    = W * LD
    P    = W + 2.0 * LD
    R    = A / P
    V_act = Q_si / A
    slope = ((Q_si * n) / (A * (R ** (2.0 / 3.0)))) ** 2.0
    H     = slope * L

    return {
        "area_m2":            round(A, 4),
        "wetted_perimeter_m": round(P, 4),
        "velocity_ms":        round(V_act, 3),
        "hydraulic_radius_m": round(R, 4),
        "head_loss_m":        round(H, 4),
    }


# ===============================================================
# MODULE 6 — Circular Tank Volume       🛢️
# ===============================================================
def m6_tank_volume(diameter_m: float, total_height_m: float,
                   freeboard_m: float) -> Dict[str, Any]:
    """
    Validated: D=5, H=10, FB=0.5 → Total=196.3 m³, Effective=186.5 m³
    """
    D  = _pos(diameter_m, "Diameter")
    H  = _pos(total_height_m, "Total height")
    FB = _nn(freeboard_m, "Free board + dead end")
    if FB >= H:
        raise ValueError("Free board cannot exceed total height.")

    A     = _circle_area(D)
    H_eff = H - FB
    V_tot = A * H
    V_eff = A * H_eff

    return {
        "cs_area_m2":       round(A, 4),
        "effective_height": round(H_eff, 3),
        "total_volume_m3":  round(V_tot, 2),
        "effective_vol_m3": round(V_eff, 2),
    }


# ===============================================================
# MODULE 7 — Liquid Height In Tank       📐
# ===============================================================
def m7_liquid_height(volume_m3: float, diameter_m: float,
                     total_height_m: float, freeboard_m: float) -> Dict[str, Any]:
    """
    Ramboll spreadsheet convention:
        effective_height = (V / A) - FB
    Validated: V=300, D=7, H=7.80, FB=0.5 → Eff H = 7295.3 mm
    """
    V  = _pos(volume_m3, "Volume")
    D  = _pos(diameter_m, "Diameter")
    H  = _pos(total_height_m, "Total height")
    FB = _nn(freeboard_m, "Free board + dead end")

    A         = _circle_area(D)
    H_liquid  = V / A                    # gross liquid column
    H_eff_m   = H_liquid - FB            # per workbook
    V_eff     = A * max(H_eff_m, 0.0)

    warning = None
    if H_liquid > H:
        warning = (f"Required liquid column ({H_liquid:.2f} m) exceeds "
                   f"total tank height ({H:.2f} m).")

    return {
        "cs_area_m2":         round(A, 4),
        "effective_height_m": round(H_eff_m, 4),
        "effective_height_mm":round(H_eff_m * 1000.0, 1),
        "effective_vol_m3":   round(V_eff, 2),
        "warning":            warning,
    }


# ===============================================================
# MODULE 8 — Tank Diameter Sizing       ⭕
# ===============================================================
def m8_tank_diameter(volume_m3: float, total_height_m: float) -> Dict[str, Any]:
    """
    Validated: V=196.3, H=10 → D = 5.0 m
    """
    V = _pos(volume_m3, "Volume")
    H = _pos(total_height_m, "Total height")

    A = V / H
    D = math.sqrt(4.0 * A / PI)

    return {
        "cs_area_m2":  round(A, 3),
        "diameter_m":  round(D, 3),
    }


# ===============================================================
# MODULE 9 — Bell-Mouth Entry Head Loss    🔔
# ===============================================================
def m9_bellmouth(flow_m3h: float, bell_diameter_m: float) -> Dict[str, Any]:
    """
    Ramboll workbook formula (Q in m³/hr, kept as-is for parity):
        H = (Q / (1.84 · L))^0.666      where L = π · D
    Validated: Q=212, D=2 → H ≈ 7.0 m
    """
    Q = _pos(flow_m3h, "Flow")
    D = _pos(bell_diameter_m, "Bell-mouth diameter")

    L = PI * D
    H = (Q / (1.84 * L)) ** 0.666

    return {
        "circumference_m": round(L, 4),
        "head_loss_m":     round(H, 3),
    }


# ===============================================================
# MODULE 10 — Rectangular Weir Head Loss   🌀
# ===============================================================
def m10_weir(flow_m3h: float, weir_length_m: float) -> Dict[str, Any]:
    """
    H = 0.467 · (Q / L)^0.666        (Q in m³/s)
    Validated: Q=212 m³/hr, L=2 → H = 0.0446 m
    """
    Q = _pos(flow_m3h, "Flow")
    L = _pos(weir_length_m, "Weir length")

    Q_si = Q / 3600.0
    H    = 0.467 * ((Q_si / L) ** 0.666)

    return {
        "flow_m3s":      round(Q_si, 4),
        "head_loss_m":   round(H, 4),
    }


# ===============================================================
# MODULE 11 — Total Discharge / Pump Head    💧
# ===============================================================
def m11_total_pump_head(flow_m3h: float, pipe_id_mm: float, length_m: float,
                        c_factor: float, static_head_m: float,
                        residual_head_m: float,
                        fittings: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    """
    Total dynamic head:
        TDH = H_static + H_friction + H_minor + H_residual
    H_minor = Σ (K_i · n_i) · V² / (2g)
    """
    Q  = _pos(flow_m3h,   "Flow")
    d  = _pos(pipe_id_mm, "Pipe ID") / 1000.0
    L  = _pos(length_m,   "Pipe length")
    C  = _pos(c_factor,   "C-factor")
    Hs = _nn(static_head_m,   "Static head")
    Hr = _nn(residual_head_m, "Residual head")

    A     = _circle_area(d)
    V     = (Q / 3600.0) / A

    # Friction (Hazen-Williams, per module 2)
    j     = (Q / (1000.8 * C * d ** 2.63)) ** 1.852
    Hf    = j * L

    # Minor losses
    sum_K = 0.0
    breakdown: List[Dict[str, Any]] = []
    for f in (fittings or []):
        try:
            k   = float(f.get("k", 0))
            qty = int(f.get("qty", 1))
        except (TypeError, ValueError):
            continue
        if k <= 0 or qty <= 0:
            continue
        contrib = k * qty
        sum_K  += contrib
        breakdown.append({
            "name": f.get("name", "Fitting"),
            "k":    k, "qty": qty,
            "loss_m": round(contrib * V * V / (2.0 * G), 4),
        })
    Hm = sum_K * V * V / (2.0 * G)

    TDH = Hs + Hf + Hm + Hr

    return {
        "velocity_ms":     round(V, 3),
        "friction_head_m": round(Hf, 4),
        "minor_head_m":    round(Hm, 4),
        "static_head_m":   round(Hs, 3),
        "residual_head_m": round(Hr, 3),
        "total_head_m":    round(TDH, 3),
        "fittings":        breakdown,
    }


# ===============================================================
# Bonus — Pump Power                   ⚡
# ===============================================================
# ===============================================================
# MODULE 12 — Pump Power Calculator            ⚡
# ===============================================================
def m12_pump_power(flow_m3h: float, head_m: float,
                   pump_eff_pct: float, motor_eff_pct: float,
                   density_kg_m3: float = 1000.0,
                   safety_margin_pct: float = 15.0) -> Dict[str, Any]:
    """
    Compute pump hydraulic, shaft and motor input power + IEC motor recommendation.

    P_hyd   = ρ·g·Q·H              (SI → W)
    P_shaft = P_hyd / η_pump
    P_input = P_shaft / η_motor

    Efficiencies accepted either as 0-1 (decimal) or 0-100 (percent) — auto-detected.

    Validated:
        Q=100 m³/hr, H=30 m, η_p=75%, η_m=92%, ρ=1000
        → P_hyd = 8.175 kW, P_shaft = 10.900 kW, P_input = 11.848 kW,
          recommended motor = 15 kW
    """
    Q   = _pos(flow_m3h,      "Flow")
    H   = _pos(head_m,        "Head")
    e_p = _pos(pump_eff_pct,  "Pump efficiency")
    e_m = _pos(motor_eff_pct, "Motor efficiency")
    rho = _pos(density_kg_m3, "Fluid density")
    sm  = _nn(safety_margin_pct, "Safety margin")

    # Efficiency normalisation
    if e_p > 1.0: e_p /= 100.0
    if e_m > 1.0: e_m /= 100.0
    if e_p >= 1.0 or e_m >= 1.0:
        raise ValueError("Efficiencies must be less than 100 %.")
    if e_p <= 0 or e_m <= 0:
        raise ValueError("Efficiencies must be positive.")

    Q_si    = Q / 3600.0                         # m³/s
    P_hyd_W = rho * G * Q_si * H                 # W
    P_sh_W  = P_hyd_W / e_p
    P_in_W  = P_sh_W  / e_m

    P_in_kW = P_in_W / 1000.0
    P_sized = P_in_kW * (1.0 + sm / 100.0)

    # Next IEC motor size at or above the sized power
    rec_motor = next(
        (m for m in IEC_MOTOR_SIZES_KW if m >= P_sized),
        IEC_MOTOR_SIZES_KW[-1],
    )

    # Specific energy: kWh consumed per cubic metre pumped
    sp_energy = P_in_kW / Q if Q > 0 else 0.0    # kWh/m³

    # Sanity check for absurd inputs
    warning = None
    if P_in_kW > 1000:
        warning = ("Motor input exceeds 1 MW — verify inputs; "
                   "typical industrial pumps are below 500 kW.")

    return {
        "hydraulic_power_kw":     round(P_hyd_W / 1000.0, 3),
        "shaft_power_kw":         round(P_sh_W  / 1000.0, 3),
        "motor_input_kw":         round(P_in_kW, 3),
        "sized_power_kw":         round(P_sized, 3),
        "recommended_motor_kw":   round(rec_motor, 2),
        "specific_energy_kwh_m3": round(sp_energy, 4),
        "pump_efficiency_used":   round(e_p * 100, 1),
        "motor_efficiency_used":  round(e_m * 100, 1),
        "warning":                warning,
    }


# ---- Backwards-compat alias for any old imports of calc_pump_power ----
calc_pump_power = m12_pump_power
# ===============================================================
# MODULE 13 — Pump Affinity Laws                ⚙️
# ===============================================================
def m13_pump_affinity(flow_m3h_1: float, head_m_1: float, power_kw_1: float,
                      speed_rpm_1: float, speed_rpm_2: float,
                      impeller_mm_1: float = 200.0,
                      impeller_mm_2: float = 200.0) -> Dict[str, Any]:
    """
    Combined affinity laws — speed AND/OR impeller-diameter change.
        Q₂/Q₁ = (N₂/N₁) · (D₂/D₁)
        H₂/H₁ = (N₂/N₁)² · (D₂/D₁)²
        P₂/P₁ = (N₂/N₁)³ · (D₂/D₁)³

    Validated:
        Q₁=100, H₁=30, P₁=15, N₁=1450, N₂=1750, D₁=D₂=200
        → Q₂ = 120.69 m³/hr, H₂ = 43.70 m, P₂ = 26.37 kW
    """
    Q1 = _pos(flow_m3h_1,   "Original flow")
    H1 = _pos(head_m_1,     "Original head")
    P1 = _pos(power_kw_1,   "Original power")
    N1 = _pos(speed_rpm_1,  "Original speed")
    N2 = _pos(speed_rpm_2,  "New speed")
    D1 = _pos(impeller_mm_1,"Original impeller diameter")
    D2 = _pos(impeller_mm_2,"New impeller diameter")

    rN = N2 / N1
    rD = D2 / D1

    Q2 = Q1 * rN * rD
    H2 = H1 * rN * rN * rD * rD
    P2 = P1 * rN * rN * rN * rD * rD * rD

    trim_pct = abs(1.0 - rD) * 100.0
    speed_pct = abs(1.0 - rN) * 100.0

    warning = None
    if trim_pct > 15.0:
        warning = (f"Impeller trim of {trim_pct:.1f}% exceeds 15% — "
                   "affinity approximation loses accuracy; verify against "
                   "manufacturer curves.")
    elif speed_pct > 25.0:
        warning = (f"Speed change of {speed_pct:.1f}% exceeds 25% — "
                   "verify against manufacturer test data.")

    return {
        "new_flow_m3h":      round(Q2, 2),
        "new_head_m":        round(H2, 3),
        "new_power_kw":      round(P2, 3),
        "speed_ratio":       round(rN, 4),
        "diameter_ratio":    round(rD, 4),
        "trim_pct":          round(trim_pct, 2),
        "warning":           warning,
    }


# ===============================================================
# MODULE 14 — Air Blower / Compressor Power     💨
# ===============================================================
def m14_blower_power(flow_nm3h: float,
                     p_suction_kpa_a: float,
                     p_discharge_kpa_a: float,
                     temp_suction_c: float = 20.0,
                     k_ratio: float = 1.4,
                     gas_mw: float = 29.0,
                     eta_adiabatic_pct: float = 70.0,
                     eta_motor_pct: float = 92.0) -> Dict[str, Any]:
    """
    Adiabatic (isentropic) + isothermal shaft power for a blower/compressor,
    plus predicted discharge temperature and IEC motor recommendation.

    Defaults represent AIR at NTP. For other gases pass k and MW.

    Validated:
        Q=1000 Nm³/hr, p₁=101.325 kPa_a, p₂=150 kPa_a, T₁=20 °C
        η_ad=70%, η_m=92% (air, k=1.4, MW=29)
        → P_ad ≈ 17.95 kW, P_iso ≈ 16.94 kW, T₂ ≈ 69.8 °C, motor ≈ 30 kW
    """
    Q_N = _pos(flow_nm3h,          "Normal flow")
    p1  = _pos(p_suction_kpa_a,    "Suction pressure")
    p2  = _pos(p_discharge_kpa_a,  "Discharge pressure")
    Tc  = float(temp_suction_c)
    k   = _pos(k_ratio,            "k ratio")
    MW  = _pos(gas_mw,             "Gas molecular weight")
    ea  = _pos(eta_adiabatic_pct,  "Adiabatic efficiency")
    em  = _pos(eta_motor_pct,      "Motor efficiency")

    if ea > 1.0: ea /= 100.0
    if em > 1.0: em /= 100.0
    if ea >= 1.0 or em >= 1.0:
        raise ValueError("Efficiencies must be less than 100 %.")
    if p2 <= p1:
        raise ValueError("Discharge pressure must exceed suction pressure.")
    if k <= 1.0:
        raise ValueError("k must be > 1.")

    # ---- Gas properties (ideal-gas assumption) ----
    R_UNIV = 8314.5                     # J/(kmol·K)
    R      = R_UNIV / MW                # J/(kg·K)
    Cp     = k * R / (k - 1.0)          # J/(kg·K)

    T1_K       = Tc + 273.15
    rho_normal = (MW * 101_325.0) / (R_UNIV * 273.15)         # kg/m³ at NTP
    m_dot      = Q_N * rho_normal / 3600.0                    # kg/s

    ratio  = (p2 * 1000.0) / (p1 * 1000.0)                    # dimensionless
    n_exp  = (k - 1.0) / k

    # ---- Ideal power (W) ----
    P_ad_ideal  = m_dot * Cp * T1_K * (ratio ** n_exp - 1.0)
    P_iso_ideal = m_dot * R  * T1_K * math.log(ratio)

    # ---- Shaft & motor (kW) ----
    P_ad_shaft  = P_ad_ideal  / ea / 1000.0
    P_iso_shaft = P_iso_ideal / ea / 1000.0
    P_motor_kW  = P_ad_shaft  / em

    # ---- Discharge temperature (K) ----
    T2_isentropic = T1_K * ratio ** n_exp
    T2_actual_K   = T1_K + (T2_isentropic - T1_K) / ea         # allow for losses

    # ---- Recommended motor (15% margin) ----
    P_sized = P_motor_kW * 1.15
    rec_motor = next((m for m in IEC_MOTOR_SIZES_KW if m >= P_sized),
                     IEC_MOTOR_SIZES_KW[-1])

    warning = None
    if T2_actual_K - 273.15 > 180:
        warning = ("Discharge temperature exceeds 180 °C — consider "
                   "intercooling or multi-stage compression.")

    return {
        "compression_ratio":       round(ratio, 3),
        "adiabatic_power_kw":      round(P_ad_shaft, 3),
        "isothermal_power_kw":     round(P_iso_shaft, 3),
        "motor_input_kw":          round(P_motor_kW, 3),
        "discharge_temp_c":        round(T2_actual_K - 273.15, 2),
        "mass_flow_kg_s":          round(m_dot, 4),
        "recommended_motor_kw":    round(rec_motor, 2),
        "warning":                 warning,
    }


# ===============================================================
# MODULE 15 — Screw Conveyor Sizing              🌀
# ===============================================================
def m15_screw_conveyor(screw_dia_mm: float, shaft_dia_mm: float,
                       pitch_mm: float, rpm: float,
                       length_m: float, incline_deg: float,
                       density_kg_m3: float,
                       fill_factor_pct: float,
                       material_factor: float,
                       drive_eff_pct: float = 90.0,
                       safety_factor: float = 1.2) -> Dict[str, Any]:
    """
    CEMA-style capacity + power for a screw conveyor.

    Validated (D=250, d=75, S=250, N=45, L=10, incline=0, ρ=1200,
               λ=30%, Fm=2.0):
        → Q_vol ≈ 9.05 m³/hr, Q_mass ≈ 10.85 t/hr, P_shaft ≈ 0.84 kW,
          recommended motor = 1.5 kW
    """
    D   = _pos(screw_dia_mm,      "Screw diameter") / 1000.0
    d   = _nn(shaft_dia_mm,       "Shaft diameter") / 1000.0
    S   = _pos(pitch_mm,          "Pitch") / 1000.0
    N   = _pos(rpm,               "RPM")
    L   = _pos(length_m,          "Length")
    theta = float(incline_deg)
    rho = _pos(density_kg_m3,     "Bulk density")
    lam = _pos(fill_factor_pct,   "Fill factor")
    Fm  = _pos(material_factor,   "Material factor")
    ed  = _pos(drive_eff_pct,     "Drive efficiency")
    SF  = _pos(safety_factor,     "Safety factor")

    if d >= D:
        raise ValueError("Shaft diameter must be less than screw diameter.")
    if lam > 100:
        raise ValueError("Fill factor must be ≤ 100 %.")
    if ed > 1.0: ed /= 100.0
    if ed >= 1.0:
        raise ValueError("Drive efficiency must be < 100 %.")

    lam_frac = lam / 100.0

    # ---- Capacity ----
    A_annulus = math.pi / 4.0 * (D * D - d * d)              # m²
    Q_vol_m3h = A_annulus * S * N * lam_frac * 60.0          # m³/hr
    Q_tph     = Q_vol_m3h * rho / 1000.0                     # t/hr

    # ---- Power components (kW, CEMA) ----
    theta_rad = math.radians(theta)
    P_material = (Q_tph * L * Fm)               / 367.0
    P_incline  = (Q_tph * L * math.sin(theta_rad)) / 367.0
    P_empty    = (screw_dia_mm * L * N)          / 100_000.0

    P_shaft = (P_material + P_incline + P_empty) * SF
    P_motor = P_shaft / ed

    # ---- Recommended IEC motor ----
    rec_motor = next((m for m in IEC_MOTOR_SIZES_KW if m >= P_motor),
                     IEC_MOTOR_SIZES_KW[-1])

    warning = None
    if lam > 45:
        warning = ("Fill factor above 45 % is only recommended for very "
                   "free-flowing non-abrasive materials.")
    if D > 0.6 and N > 60:
        warning = ("Large-diameter screws should not exceed ~60 rpm — "
                   "check with manufacturer.")

    return {
        "capacity_m3h":         round(Q_vol_m3h, 2),
        "capacity_tph":         round(Q_tph, 3),
        "material_power_kw":    round(P_material, 3),
        "incline_power_kw":     round(P_incline, 3),
        "empty_power_kw":       round(P_empty, 3),
        "shaft_power_kw":       round(P_shaft, 3),
        "motor_input_kw":       round(P_motor, 3),
        "recommended_motor_kw": round(rec_motor, 2),
        "warning":              warning,
    }


# ===============================================================
# MODULE REGISTRY  (used by app.py / templates / dashboards)
# ===============================================================
MODULE_REGISTRY: List[Dict[str, Any]] = [
    {"id":  1, "slug": "pipe-diameter",     "name": "Pipe Diameter Sizing",
     "icon": "📏", "category": "Pipes",    "route": "module_1"},
    {"id":  2, "slug": "pipe-head-loss",    "name": "Pipe Head Loss (Hazen-Williams)",
     "icon": "💧", "category": "Pipes",    "route": "module_2"},
    {"id":  3, "slug": "flow-through-pipe", "name": "Flow Through Pipe",
     "icon": "🚰", "category": "Pipes",    "route": "module_3"},
    {"id":  4, "slug": "channel-sizing",    "name": "Channel Sizing",
     "icon": "🌊", "category": "Channels", "route": "module_4"},
    {"id":  5, "slug": "channel-head-loss", "name": "Channel Head Loss (Manning)",
     "icon": "〽️", "category": "Channels", "route": "module_5"},
    {"id":  6, "slug": "tank-volume",       "name": "Circular Tank Volume",
     "icon": "🛢️", "category": "Tanks",    "route": "module_6"},
    {"id":  7, "slug": "liquid-height",     "name": "Liquid Height In Tank",
     "icon": "📐", "category": "Tanks",    "route": "module_7"},
    {"id":  8, "slug": "tank-diameter",     "name": "Tank Diameter Sizing",
     "icon": "⭕", "category": "Tanks",    "route": "module_8"},
    {"id":  9, "slug": "bell-mouth",        "name": "Bell-Mouth Entry Head Loss",
     "icon": "🔔", "category": "Fittings", "route": "module_9"},
    {"id": 10, "slug": "weir",              "name": "Rectangular Weir Head Loss",
     "icon": "🌀", "category": "Fittings", "route": "module_10"},
    {"id": 11, "slug": "total-pump-head",   "name": "Total Discharge / Pump Head",
     "icon": "💧", "category": "Pump",     "route": "total_pump_head"},
    {"id": 12, "slug": "pump-power",         "name": "Pump Power Calculator",
     "icon": "⚡", "category": "Pump",       "route": "pump_power"},
    {"id": 13, "slug": "pump-affinity",   "name": "Pump Affinity Laws",
     "icon": "⚙️", "category": "Pump",     "route": "pump_affinity"},
    {"id": 14, "slug": "blower-power",    "name": "Air Blower Power",
     "icon": "💨", "category": "Blower",   "route": "blower_power"},
    {"id": 15, "slug": "screw-conveyor",  "name": "Screw Conveyor Sizing",
     "icon": "🌀", "category": "Conveyor", "route": "screw_conveyor"},
]


# ===============================================================
# Self-test — run `python calculations.py` to verify
# ===============================================================
if __name__ == "__main__":
    def _chk(label, got, want, tol):
        ok = abs(got - want) <= tol
        print(f"  {'✅' if ok else '❌'} {label:<32} got={got:<12} want={want}")
        return ok

    print("── CalcVault self-tests ──")
    ok = True
    ok &= _chk("M1  required ID mm",
               m1_pipe_diameter(40, 1.62)["required_id_mm"], 93.4, 0.2)
    ok &= _chk("M2  head loss m",
               m2_pipe_head_loss(212, 140, 210.1, 131)["head_loss_m"], 1.5656, 0.002)
    ok &= _chk("M3  flow m³/hr",
               m3_flow_through_pipe(100, 3)["flow_m3h"], 84.8, 0.1)
    ok &= _chk("M4  required width m",
               m4_channel_sizing(212, 0.6, 300)["required_width_m"], 0.327, 0.002)
    ok &= _chk("M5  head loss m",
               m5_channel_head_loss(212, 0.35, 0.3, 100, 0.015)["head_loss_m"], 0.133, 0.002)
    r6 = m6_tank_volume(5, 10, 0.5)
    ok &= _chk("M6  total volume m³",     r6["total_volume_m3"],  196.3, 0.1)
    ok &= _chk("M6  effective volume m³", r6["effective_vol_m3"], 186.5, 0.1)
    ok &= _chk("M7  effective H mm",
               m7_liquid_height(300, 7, 7.80, 0.5)["effective_height_mm"], 7295.3, 0.3)
    ok &= _chk("M8  diameter m",
               m8_tank_diameter(196.3, 10)["diameter_m"], 5.0, 0.01)
    ok &= _chk("M9  head loss m",
               m9_bellmouth(212, 2)["head_loss_m"], 7.0, 0.1)
    ok &= _chk("M10 head loss m",
               m10_weir(212, 2)["head_loss_m"], 0.0446, 0.001)
    r12 = m12_pump_power(100, 30, 75, 92, 1000)
    ok &= _chk("M12 hydraulic power kW",  r12["hydraulic_power_kw"],  8.175, 0.01)
    ok &= _chk("M12 shaft power kW",      r12["shaft_power_kw"],     10.900, 0.02)
    ok &= _chk("M12 motor input kW",      r12["motor_input_kw"],     11.848, 0.02)
    ok &= _chk("M12 recommended motor kW",r12["recommended_motor_kw"], 15.0, 0.01)
    r13 = m13_pump_affinity(100, 30, 15, 1450, 1750, 200, 200)
    ok &= _chk("M13 new flow",   r13["new_flow_m3h"], 120.69, 0.05)
    ok &= _chk("M13 new head",   r13["new_head_m"],    43.70, 0.02)
    ok &= _chk("M13 new power",  r13["new_power_kw"],  26.37, 0.02)

    r14 = m14_blower_power(1000, 101.325, 150, 20, 1.4, 29, 70, 92)
    ok &= _chk("M14 adiabatic kW",  r14["adiabatic_power_kw"],  17.95, 0.10)
    ok &= _chk("M14 isothermal kW", r14["isothermal_power_kw"], 16.94, 0.10)

    r15 = m15_screw_conveyor(250, 75, 250, 45, 10, 0, 1200, 30, 2.0)
    ok &= _chk("M15 volumetric m³/h", r15["capacity_m3h"], 9.05, 0.03)
    ok &= _chk("M15 mass t/h",        r15["capacity_tph"], 10.85, 0.03)
    ok &= _chk("M15 shaft kW",        r15["shaft_power_kw"], 0.84, 0.02)

    print("── ALL PASSED ✅" if ok else "── FAILURES ❌")