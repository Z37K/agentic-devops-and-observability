"""
account_state_check.py - deterministic live-account sanity checks.

Why this exists
---------------
The execution code was built at high pace with an LLM. That speed is the whole point - but
some coding errors still reached production (e.g. a stack/exit bug that left a position open,
or fired the wrong way). These are reliable checks on the ACTUAL account state that
flag those symptoms so the operator can jump in. It does NOT place, modify, or gate trades -
it only reports what looks wrong.

Run a demo on a sample state (no broker needed):
    python account_state_check.py
Use live (optional; needs MetaTrader5 + a running terminal):
    from account_state_check import check_state, snapshot_from_mt5
    for sev, msg in check_state(snapshot_from_mt5(), intended): print(sev, msg)
"""

import sys


def check_state(state: dict, intended: dict) -> list[tuple[str, str]]:
    """Return a list of (severity, message) flags. Empty list == nothing looks wrong.

    state    : what the account actually is right now (from MT5 or a snapshot dict).
    intended : what the strategy says it should be (symbol, direction, limits).
    """
    flags: list[tuple[str, str]] = []
    positions = state.get("positions", [])

    # 1. Orphaned positions: should be flat (post-lock / past cutoff) but isn't.
    if state.get("should_be_flat") and positions:
        flags.append(("CRITICAL", f"Should be flat but {len(positions)} position(s) still open - "
                                   "likely an exit/flatten that didn't complete."))

    # 2. Unconfirmed orders: sent but no fill came back (the 'assumed success' bug).
    unconfirmed = state.get("unconfirmed_orders", 0)
    if unconfirmed:
        flags.append(("CRITICAL", f"{unconfirmed} order(s) sent without a confirmed fill - "
                                   "do not assume they executed."))

    for p in positions:
        # 3. Wrong symbol.
        if p.get("symbol") != intended.get("symbol"):
            flags.append(("WARN", f"Position on {p.get('symbol')} but strategy symbol is "
                                  f"{intended.get('symbol')}."))
        # 4. Wrong direction (fired the wrong way).
        if intended.get("direction") and p.get("direction") != intended["direction"]:
            flags.append(("CRITICAL", f"Position direction {p.get('direction')} != intended "
                                      f"{intended['direction']} on {p.get('symbol')}."))

    # 5. Too many positions (netting account should hold one net position per symbol).
    max_positions = intended.get("max_positions", 1)
    if len(positions) > max_positions:
        flags.append(("WARN", f"{len(positions)} positions open but expected <= {max_positions} "
                              "- possible stacking/duplication bug."))

    # 6. Leverage outside intended range.
    lev = state.get("leverage")
    if lev is not None:
        hard_max = intended.get("hard_max_leverage", 28.0)
        target = intended.get("target_leverage")
        if lev > hard_max:
            flags.append(("CRITICAL", f"Leverage {lev:.1f}x exceeds the hard limit {hard_max:.0f}x."))
        elif target and lev > target * 1.25:
            flags.append(("WARN", f"Leverage {lev:.1f}x overshoots target {target:.1f}x by >25% "
                                  "- check the stacking loop."))

    return flags


def snapshot_from_mt5() -> dict:
    """Build a state dict from a live MetaTrader5 terminal. Optional; needs MetaTrader5."""
    import MetaTrader5 as mt5  # imported lazily so the module loads without MT5
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")
    acct = mt5.account_info()
    positions = [
        {"symbol": p.symbol, "direction": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
         "volume": p.volume}
        for p in (mt5.positions_get() or [])
    ]
    gross_notional = sum(abs(p.volume) * (mt5.symbol_info_tick(p["symbol"]).ask) for p in positions)
    return {
        "positions": positions,
        "leverage": (gross_notional / acct.equity) if acct and acct.equity else None,
        "should_be_flat": False,
        "unconfirmed_orders": 0,
    }


if __name__ == "__main__":
    # Demo: a state that contains two real coding-error symptoms (wrong-way + overshoot).
    intended = {"symbol": "BTCUSD", "direction": "sell", "max_positions": 1,
                "target_leverage": 8.0, "hard_max_leverage": 28.0}
    sample_state = {
        "positions": [{"symbol": "BTCUSD", "direction": "buy", "volume": 100.0}],  # wrong way!
        "leverage": 11.5,            # overshot the 8x target
        "should_be_flat": False,
        "unconfirmed_orders": 1,     # an order with no confirmed fill
    }

    print("# account_state_check demo (sample state with seeded errors)\n")
    flags = check_state(sample_state, intended)
    if not flags:
        print("OK - nothing looks wrong.")
    for sev, msg in flags:
        print(f"[{sev}] {msg}")
    sys.exit(1 if any(s == "CRITICAL" for s, _ in flags) else 0)
