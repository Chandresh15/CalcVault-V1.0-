"""
units.py — CalcVault (Ramboll Edition)
======================================
Unit conversion for the flexible input selectors used across all
hydraulic modules.

Design:
  • Every quantity has ONE SI base unit.
  • Each alias stores a multiplier to that base:
        value_in_base = value_in_alias * factor
        value_in_alias = value_in_base / factor
  • No chained conversions → no compounded floating-point drift.

Public API:
    to_si(value, unit)                 → float in SI base
    from_si(value_si, unit)            → float in requested unit
    convert(value, from_unit, to_unit) → float
    units_for(quantity)                → list of alias strings
    quantity_of(unit)                  → 'flow' | 'velocity' | ...
    format_smart(value_si, quantity, decimals=3) → "12.34 L/s"

No Flask / DB / I/O — pure math, unit-testable via `python units.py`.
"""

from __future__ import annotations
from typing import Dict, List, Tuple

# ---------------------------------------------------------------
# Base units (SI)
# ---------------------------------------------------------------
BASE_UNITS: Dict[str, str] = {
    "flow":     "m3_s",     # cubic metres per second
    "velocity": "m_s",      # metres per second
    "length":   "m",        # metres  (also used for diameter, head)
    "volume":   "m3",       # cubic metres
    "power":    "W",        # watts
    "mass":     "kg",       # kilograms
}

# ---------------------------------------------------------------
# Alias table
#   quantity -> { alias_symbol: (label, factor_to_base) }
# The FIRST entry per quantity is treated as the canonical display.
# ---------------------------------------------------------------
UNITS: Dict[str, Dict[str, Tuple[str, float]]] = {

    # ---- Volumetric flow (base: m³/s) ----
    "flow": {
        "m3_h":  ("m³/hr",  1.0 / 3600.0),
        "m3_s":  ("m³/s",   1.0),
        "l_s":   ("L/s",    1.0e-3),
        "l_min": ("L/min",  1.0e-3 / 60.0),
        "gpm":   ("US GPM", 3.785411784e-3 / 60.0),  # US gallon = 3.785411784 L
        "igpm":  ("UK GPM", 4.54609e-3 / 60.0),      # Imperial gallon
        "cfs":   ("ft³/s",  0.028316846592),
        "mgd":   ("MGD",    3.785411784e-3 * 1e6 / 86400.0),   # US million gal/day
    },

    # ---- Linear velocity (base: m/s) ----
    "velocity": {
        "m_s":   ("m/s",   1.0),
        "cm_s":  ("cm/s",  0.01),
        "km_h":  ("km/h",  1000.0 / 3600.0),
        "ft_s":  ("ft/s",  0.3048),
        "fpm":   ("ft/min",0.3048 / 60.0),
        "mph":   ("mph",   0.44704),
    },

    # ---- Length / diameter / head (base: m) ----
    "length": {
        "m":     ("m",   1.0),
        "mm":    ("mm",  1.0e-3),
        "cm":    ("cm",  1.0e-2),
        "km":    ("km",  1000.0),
        "in":    ("in",  0.0254),
        "ft":    ("ft",  0.3048),
    },

    # ---- Volume (base: m³) ----
    "volume": {
        "m3":    ("m³",  1.0),
        "l":     ("L",   1.0e-3),
        "ml":    ("mL",  1.0e-6),
        "gal_us":("US gal", 3.785411784e-3),
        "gal_uk":("UK gal", 4.54609e-3),
        "ft3":   ("ft³", 0.028316846592),
    },

    # ---- Power (base: W) ----
    "power": {
        "W":   ("W",   1.0),
        "kW":  ("kW",  1000.0),
        "MW":  ("MW",  1.0e6),
        "HP":  ("HP",  745.6998715822702),   # mechanical horsepower
        "PS":  ("PS",  735.49875),           # metric horsepower
    },

    # ---- Mass (base: kg) — for pump / motor weights ----
    "mass": {
        "kg":  ("kg", 1.0),
        "g":   ("g",  1.0e-3),
        "t":   ("t",  1000.0),
        "lb":  ("lb", 0.45359237),
    },
}

# ---------------------------------------------------------------
# Reverse lookup:  alias -> quantity  (built once, O(1) lookup)
# ---------------------------------------------------------------
_ALIAS_TO_QTY: Dict[str, str] = {
    alias: qty
    for qty, table in UNITS.items()
    for alias in table.keys()
}


# ===============================================================
# Public API
# ===============================================================
def quantity_of(unit: str) -> str:
    """Return the quantity name ('flow', 'velocity', ...) for an alias."""
    if unit not in _ALIAS_TO_QTY:
        raise ValueError(f"Unknown unit alias: {unit!r}")
    return _ALIAS_TO_QTY[unit]


def units_for(quantity: str) -> List[str]:
    """List of alias symbols available for a quantity — UI dropdown source."""
    if quantity not in UNITS:
        raise ValueError(f"Unknown quantity: {quantity!r}")
    return list(UNITS[quantity].keys())


def label_of(unit: str) -> str:
    """Human display label for an alias, e.g. 'm3_h' → 'm³/hr'."""
    qty = quantity_of(unit)
    return UNITS[qty][unit][0]


def to_si(value: float, unit: str) -> float:
    """Convert a value from any alias to its SI base unit."""
    qty     = quantity_of(unit)
    _, fact = UNITS[qty][unit]
    return float(value) * fact


def from_si(value_si: float, unit: str) -> float:
    """Convert a value in SI base to the requested alias."""
    qty     = quantity_of(unit)
    _, fact = UNITS[qty][unit]
    return float(value_si) / fact


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert directly between any two aliases of the same quantity."""
    q_from = quantity_of(from_unit)
    q_to   = quantity_of(to_unit)
    if q_from != q_to:
        raise ValueError(
            f"Cannot convert {from_unit!r} ({q_from}) → {to_unit!r} ({q_to})"
        )
    return from_si(to_si(value, from_unit), to_unit)


# ---------------------------------------------------------------
# format_smart — auto-pick the most readable unit for display
# ---------------------------------------------------------------
# Ordered candidate ladders (large → small).  format_smart() walks
# the ladder and picks the first unit that yields a number ≥ 1.
# ---------------------------------------------------------------
_SMART_LADDER: Dict[str, List[str]] = {
    "flow":     ["m3_h", "l_s", "l_min"],           # engineering-friendly order
    "velocity": ["m_s", "cm_s"],
    "length":   ["km", "m", "cm", "mm"],
    "volume":   ["m3", "l", "ml"],
    "power":    ["MW", "kW", "W"],
    "mass":     ["t", "kg", "g"],
}


def format_smart(value_si: float, quantity: str, decimals: int = 3) -> str:
    """
    Return a human-readable string using the most practical unit.
    Example: format_smart(0.00030, 'volume') → '0.300 L'
             format_smart(8500,    'power')  → '8.500 kW'
    """
    if quantity not in _SMART_LADDER:
        raise ValueError(f"No smart-format ladder for quantity: {quantity!r}")
    v = float(value_si)
    if v == 0:
        best = _SMART_LADDER[quantity][-1]        # smallest unit for zero
        return f"0 {label_of(best)}"

    for alias in _SMART_LADDER[quantity]:
        converted = from_si(abs(v), alias)
        if converted >= 1.0:
            return f"{from_si(v, alias):.{decimals}f} {label_of(alias)}"

    # Everything was < 1 → use the smallest unit anyway
    alias = _SMART_LADDER[quantity][-1]
    return f"{from_si(v, alias):.{decimals}f} {label_of(alias)}"


# ---------------------------------------------------------------
# Convenience shortcuts used by calculations / templates
# ---------------------------------------------------------------
def flow_to_m3h(value: float, unit: str = "m3_h") -> float:
    """UI helper — return flow in m³/hr regardless of input alias."""
    return from_si(to_si(value, unit), "m3_h")


def velocity_to_ms(value: float, unit: str = "m_s") -> float:
    return to_si(value, unit)


def length_to_m(value: float, unit: str = "m") -> float:
    return to_si(value, unit)


# ===============================================================
# Self-test  →  python units.py
# ===============================================================
if __name__ == "__main__":
    def _chk(label, got, want, tol=1e-6):
        ok = abs(got - want) <= tol
        print(f"  {'✅' if ok else '❌'} {label:<40} got={got:.6g}  want={want}")
        return ok

    print("── units.py self-tests ──")
    ok = True

    # Flow
    ok &= _chk("40 m³/hr → m³/s",
               to_si(40, "m3_h"),           40 / 3600.0)
    ok &= _chk("1 m³/s → GPM (US)",
               from_si(1.0, "gpm"),         15850.323140625, tol=1e-3)
    ok &= _chk("100 L/s → m³/hr",
               convert(100, "l_s", "m3_h"), 360.0, tol=1e-9)

    # Velocity
    ok &= _chk("36 km/h → m/s",     to_si(36, "km_h"),    10.0)
    ok &= _chk("10 ft/s → m/s",     to_si(10, "ft_s"),    3.048)

    # Length
    ok &= _chk("210.1 mm → m",      to_si(210.1, "mm"),   0.2101)
    ok &= _chk("12 in → mm",        convert(12, "in", "mm"), 304.8, tol=1e-9)

    # Volume
    ok &= _chk("300 L → m³",        to_si(300, "l"),      0.300)

    # Power
    ok &= _chk("5 kW → W",          to_si(5, "kW"),       5000.0)
    ok &= _chk("1 HP → W",          to_si(1, "HP"),       745.6998715822702)

    # Cross-quantity guard
    try:
        convert(1, "m_s", "l_s")
        ok = False
        print("  ❌ cross-quantity did NOT raise")
    except ValueError:
        print("  ✅ cross-quantity raises ValueError")

    # Smart format
    print(f"     format_smart(0.00030 m³)  → {format_smart(0.00030, 'volume')}")
    print(f"     format_smart(8500 W)      → {format_smart(8500,   'power')}")
    print(f"     format_smart(0.0025 m)    → {format_smart(0.0025, 'length')}")

    print("── ALL PASSED ✅" if ok else "── FAILURES ❌")