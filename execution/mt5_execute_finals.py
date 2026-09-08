"""
mt5_execute_finals.py  -  FINAL-DAY executor (combination return+Sharpe lock).

We're at +$45.46k = borderline Top-25. ONE more barrier / optional-stopping trade, but the EXIT is
a COMBINATION (score) trigger, not a fixed equity target:
  selective strong-12h entry (dir = sign(mom)) -> stack toward 8x (best-effort fills)
  -> LOCK the instant the PROJECTED FINAL SCORE clears Top-25 (return-rank + estimated Sharpe-rank
     + locked drawdown-rank), i.e. take a winning return/Sharpe combo early -- accepting the bleed
  -> STOP at $750,000 (loose / DQ-only floor: ~3% adverse BTC move at 8x; forced-liq far below)
  -> snapshot-flatten 10 min before cutoff.

Sharpe handling (all ESTIMATED -- the true full-finals 15-min Sharpe isn't reconstructable live):
  - est full Sharpe = sample-weighted blend of BASE_SHARPE (existing finals samples) and THIS trade's
    live 15-min-equity Sharpe.
  - post-lock BLEED projected via *sqrt(n/(n+flat_tail))* (flattening adds zero-return samples).
  - minus a small SHARPE_BUFFER (0.01) -- Sharpe is only 10% of score and our estimate is rough.
  We only LOCK when the combo is projected to STILL clear Top-25 at the snapshot.

Flat book first (MT5 Close, or `python mt5_execute_finals.py flatten`). Then:
  python mt5_execute_finals.py            # PAPER dry-run
  python mt5_execute_finals.py live       # ARMED
"""
import time, math, csv, os, statistics, datetime as dt
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

# ===================== CONFIG =====================
MODE        = "paper"
ARMED       = False
SYMBOL      = "BTCUSD"
TARGET_LEV  = 8.0                # stack toward this (one order caps ~5.9x). Lower = smoother (better Sharpe).
STRONG_MOM  = 0.015              # |12h momentum| entry gate
TREND_BARS  = 720                # 12h on M1
DIRECTION_OVERRIDE = 0

# --- combination (return + Sharpe) LOCK ---
LOCK_SCORE   = 80.6              # projected final score to lock = clean Top-25 (rank-25 line=80.5 + tiny cushion)
MIN_LOCK_EQ  = 1_055_000.0       # don't lock below +$55k (so a lock is never a pure-Sharpe fluke)
STOP_EQ      = 750_000.0         # loose / DQ-only floor (~3% adverse BTC move at 8x; forced-liq far below)
BASE_SHARPE  = 0.11              # est. existing full-finals Sharpe (MT5 ~0.12; comp 15-min likely ~0.10)
N_BASE       = 150               # est. existing finals 15-min samples (~38h in @ 4/h)
SHARPE_BUFFER = 0.01             # discount est Sharpe (10% component + rough estimate) -- user's buffer
DDR_LOCKED   = 56.0              # our locked finals drawdown rank (~8% equity DD; can't improve)
BOARD_CSV    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leaderboard_1830.csv")   # anonymised field snapshot

SNAP_BUFFER_MIN = -60            # negative = HARD_EXIT fires after snapshot; position stays open through 22:00
FINALS_END_UTC  = dt.datetime(2026, 6, 26, 21, 0, 0, tzinfo=dt.timezone.utc)  # 22:00 BST -- CONFIRM
PER_ORDER_LOTS  = 100.0
STACK_SLICE_SEC = 8
STACK_STALE_MAX = 3
POLL_SEC    = 5
SHARPE_SEC  = 900                # 15-min equity sampling (matches competition Sharpe, rules §5)
MAGIC       = 770028
DEVIATION   = 50
EQUITY_LOG  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finals_equity_log_finalday.csv")
# ==================================================

PAPER     = (MODE == "paper")
HARD_EXIT = FINALS_END_UTC - dt.timedelta(minutes=SNAP_BUFFER_MIN)

def log(m): print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}Z] {m}", flush=True)
def need_mt5():
    if mt5 is None: raise SystemExit("MetaTrader5 not installed (pip install MetaTrader5).")
    if not mt5.initialize(): raise SystemExit(f"mt5.initialize() failed: {mt5.last_error()}")
def mid(s): t = mt5.symbol_info_tick(s); return (t.ask + t.bid) / 2.0
def info(s):
    si = mt5.symbol_info(s)
    if si and not si.visible: mt5.symbol_select(s, True); si = mt5.symbol_info(s)
    return si

def load_board():
    """Round-3 board = field proxy for percentile ranks (finals is blinded)."""
    pnls, shps = [], []
    for r in csv.DictReader(open(BOARD_CSV, encoding="utf-8")):
        pnls.append(float(r["pnl"])); shps.append(float(r["sharpe"]))
    return sorted(pnls), sorted(shps), len(pnls)

def mom12(symbol):
    r = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, TREND_BARS + 1)
    if r is None or len(r) < TREND_BARS: return None
    return r[-1]["close"] / r[0]["close"] - 1.0

def get_pos(symbol):
    poss = mt5.positions_get(symbol=symbol) or []
    return next((p for p in poss if p.magic == MAGIC), poss[0] if poss else None)

def wait_for_fill(symbol, prev_vol, tries=20, delay=0.3):
    for _ in range(tries):
        p = get_pos(symbol); v = p.volume if p else 0.0
        if v > prev_vol + 1e-6: return p
        time.sleep(delay)
    return get_pos(symbol)

def send_add(symbol, d, lots):
    if not ARMED:
        log(f"(paper) would add {lots:.2f} lots"); return None
    t = mt5.symbol_info_tick(symbol)
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": round(lots, 2),
           "type": mt5.ORDER_TYPE_BUY if d > 0 else mt5.ORDER_TYPE_SELL,
           "price": t.ask if d > 0 else t.bid, "deviation": DEVIATION, "magic": MAGIC,
           "type_filling": mt5.ORDER_FILLING_IOC, "comment": "finalday-stack"}
    return mt5.order_send(req)

def stack_to_target(symbol, d, equity):
    """Build toward TARGET_LEV via repeated orders. Idempotent (reads live vol, adds only the gap);
    hard-stops on stale fills / time so it can't run away. Best-effort."""
    si = info(symbol); cz = si.trade_contract_size; step = si.volume_step or 0.01
    target = math.floor((TARGET_LEV * equity / (cz * mid(symbol))) / step) * step
    log(f"stacking toward {TARGET_LEV:.0f}x = {target:.2f} lots {'SHORT' if d < 0 else 'LONG'} ...")
    stale = 0
    while dt.datetime.now(dt.timezone.utc) < HARD_EXIT:
        p = get_pos(symbol); cur = p.volume if p else 0.0
        if cur >= target - step / 2:
            log(f"target reached: {cur:.2f} lots"); break
        want = math.floor(min(PER_ORDER_LOTS, target - cur) / step) * step
        if want < (si.volume_min or 0.01): break
        log(f"  order {want:.2f} lots (have {cur:.2f}/{target:.2f})")
        res = send_add(symbol, d, want)
        if res is not None and res.retcode != mt5.TRADE_RETCODE_DONE:
            log(f"  retcode={res.retcode} {res.comment}")
        p2 = wait_for_fill(symbol, cur); new = p2.volume if p2 else 0.0
        if new <= cur + step / 2:
            stale += 1; log(f"  no fill ({stale}/{STACK_STALE_MAX}) - thin liquidity")
            if stale >= STACK_STALE_MAX: log("  stopping stack: liquidity exhausted"); break
        else:
            stale = 0; log(f"  -> {new:.2f} lots")
        time.sleep(STACK_SLICE_SEC)
    return get_pos(symbol)

def set_broker_sl(symbol, p):
    """Server-side SL at the price where EQUITY = STOP_EQ (breakeven floor). Off the live position."""
    if not ARMED: return None
    si = info(symbol); cz = si.trade_contract_size
    bal = mt5.account_info().balance
    d = 1 if p.type == mt5.POSITION_TYPE_BUY else -1
    denom = d * p.volume * cz
    if abs(denom) < 1e-9: return None
    sl = round(p.price_open + (STOP_EQ - bal) / denom, si.digits)
    if abs((p.sl or 0.0) - sl) > si.point * 5:
        mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol,
                        "position": p.ticket, "sl": sl, "tp": 0.0})
        log(f"broker SL @ {sl:.2f}  (= breakeven floor, equity {STOP_EQ:,.0f}; server-side DQ/blowup guard)")
    return sl

def flatten(symbol, reason):
    """Close the WHOLE position, retrying in <=100-lot chunks until truly flat (partial-fill safe).
    THE FIX vs last time: the old version fired ONE IOC order and returned -> a partial fill orphaned lots."""
    if not ARMED:
        p = get_pos(symbol); log(f"(paper) would flatten {p.volume if p else 0:.2f} lots ({reason})"); return
    deadline = time.time() + 180; stale = 0
    log(f"FLATTEN ({reason}) starting ...")
    while time.time() < deadline:
        p = get_pos(symbol)
        if not p: log(f"  FLAT ({reason}) - fully closed."); return
        si = info(symbol); step = si.volume_step or 0.01
        d = 1 if p.type == mt5.POSITION_TYPE_BUY else -1
        vol = math.floor(min(p.volume, PER_ORDER_LOTS) / step) * step
        t = mt5.symbol_info_tick(symbol); before = p.volume
        res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": vol,
                              "type": mt5.ORDER_TYPE_SELL if d > 0 else mt5.ORDER_TYPE_BUY,
                              "price": t.bid if d > 0 else t.ask, "position": p.ticket,
                              "deviation": DEVIATION, "magic": MAGIC,
                              "type_filling": mt5.ORDER_FILLING_IOC, "comment": f"close-{reason}"})
        time.sleep(1.0)
        p2 = get_pos(symbol); after = p2.volume if p2 else 0.0
        if after >= before - step / 2:
            stale += 1
            log(f"  close no-fill ({stale}/5) retcode={getattr(res,'retcode',None)} {getattr(res,'comment','')}")
            if stale >= 5:
                log(f"  !!! FLATTEN INCOMPLETE ({reason}): {after:.2f} lots STILL OPEN - CHECK MT5 NOW"); return
        else:
            stale = 0; log(f"  closing ({reason}): {after:.2f} lots remaining")
        time.sleep(2)
    log(f"  !!! FLATTEN TIMED OUT ({reason}) - CHECK MT5 for remaining lots")

class Sharpe:
    """This trade's live 15-min EQUITY Sharpe (rules §5). Blended with BASE_SHARPE for the full estimate."""
    def __init__(s): s.samp = []; s.last = 0.0
    def update(s, eq, ts):
        if ts - s.last >= SHARPE_SEC:
            s.samp.append(eq); s.last = ts
    def value(s):
        n = len(s.samp)
        if n < 3: return None, 0
        r = [s.samp[i] / s.samp[i-1] - 1 for i in range(1, n) if s.samp[i-1] > 0]
        if len(r) < 2 or statistics.pstdev(r) == 0: return 0.0, len(r)
        return statistics.mean(r) / statistics.pstdev(r), len(r)

def wait_for_signal():
    if DIRECTION_OVERRIDE in (1, -1):
        log(f"OVERRIDE: entering {'LONG' if DIRECTION_OVERRIDE > 0 else 'SHORT'} now"); return DIRECTION_OVERRIDE
    log(f"waiting for strong signal: |12h mom {SYMBOL}| >= {STRONG_MOM*100:.2f}% ...")
    while dt.datetime.now(dt.timezone.utc) < HARD_EXIT:
        m = mom12(SYMBOL)
        if m is None: time.sleep(POLL_SEC); continue
        if abs(m) >= STRONG_MOM:
            d = 1 if m > 0 else -1
            log(f"SIGNAL FIRED: 12h mom={m*100:+.2f}% -> {'LONG' if d > 0 else 'SHORT'}"); return d
        log(f"  12h mom={m*100:+.2f}% (need |.|>={STRONG_MOM*100:.2f}%)"); time.sleep(POLL_SEC)
    return 0

def manage(symbol):
    sh = Sharpe(); last_vol = None
    pnls, shps, Nb = load_board()
    rankR = lambda lst, v: 100.0 * (Nb - (sum(1 for x in lst if x > v) + 1)) / (Nb - 1)
    with open(EQUITY_LOG, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["ts_utc","price","equity","ret","lots","lev",
                                       "trade_sharpe","est_sharpe","proj_score"])
        while True:
            now = dt.datetime.now(dt.timezone.utc); ts = now.timestamp(); px = mid(symbol)
            p = get_pos(symbol)
            if p is None: log("position CLOSED (manual / SL / lock) -> ending cleanly."); break
            si = info(symbol); cz = si.trade_contract_size
            eq = mt5.account_info().equity; pnl = eq - 1_000_000.0; ret = eq / 1_000_000 - 1
            lev = p.volume * cz * px / eq if eq > 0 else 0.0
            sh.update(eq, ts); tsh, ntr = sh.value()
            # est full-finals Sharpe = sample-weighted blend of baseline + this trade
            if tsh is None: est = BASE_SHARPE; ncur = N_BASE
            else: ncur = N_BASE + ntr; est = (N_BASE * BASE_SHARPE + ntr * tsh) / ncur
            # project the post-lock BLEED (flat tail adds zero-return samples): Sharpe ~*= sqrt(n/(n+flat))
            hrs = max(0.0, (FINALS_END_UTC - now).total_seconds() / 3600.0)
            n_flat = hrs * (3600.0 / SHARPE_SEC)
            proj_sharpe = est * math.sqrt(ncur / (ncur + n_flat)) - SHARPE_BUFFER   # +conservative buffer
            proj_score = 0.70*rankR(pnls, pnl) + 0.15*DDR_LOCKED + 0.10*rankR(shps, proj_sharpe) + 0.05*100
            if last_vol is None or abs(p.volume - last_vol) > (si.volume_step or 0.01):
                set_broker_sl(symbol, p); last_vol = p.volume
            w.writerow([f"{now:%Y-%m-%d %H:%M:%S}", f"{px:.2f}", f"{eq:.2f}", f"{ret:.4f}",
                        f"{p.volume:.2f}", f"{lev:.2f}", "" if tsh is None else f"{tsh:.3f}",
                        f"{est:.3f}", f"{proj_score:.2f}"]); f.flush()
            log(f"px={px:.2f} eq={eq:,.0f} ({ret:+.2%}) lev={lev:.2f}x trade_shp={'n/a' if tsh is None else f'{tsh:.3f}'} "
                f"est_shp={est:.3f} -> PROJ SCORE {proj_score:.2f} (lock>={LOCK_SCORE})")
            reason = None
            if now >= HARD_EXIT:                                   reason = "snapshot-flat"
            elif eq <= STOP_EQ:                                    reason = "STOP (breakeven floor)"
            elif proj_score >= LOCK_SCORE and eq >= MIN_LOCK_EQ:   reason = f"LOCK combo (proj {proj_score:.1f} = clean Top-25)"
            if reason: flatten(symbol, reason); break
            time.sleep(POLL_SEC)
    log(f"done. equity+score path -> {EQUITY_LOG}")

def main():
    need_mt5()
    acc = mt5.account_info(); start_eq = acc.equity
    log(f"FINAL-DAY | acct {acc.login} eq={start_eq:,.2f} | target {TARGET_LEV:.0f}x {SYMBOL} MAGIC={MAGIC} MODE={MODE}")
    log(f"exits: LOCK proj-score>={LOCK_SCORE} (return+est Sharpe, -{SHARPE_BUFFER} buf, bleed-projected) & eq>={MIN_LOCK_EQ:,.0f} "
        f"| STOP eq<={STOP_EQ:,.0f} (breakeven) | hard-flat {HARD_EXIT:%H:%MZ}")

    p = get_pos(SYMBOL)
    if p is not None:
        d = 1 if p.type == mt5.POSITION_TYPE_BUY else -1
        log(f"managing EXISTING {SYMBOL}: {'LONG' if d>0 else 'SHORT'} {p.volume:.2f} lots @ {p.price_open:.2f} (MANAGE-ONLY)")
        if PAPER: log("[PAPER] would manage to combo-LOCK / STOP. dry-run only."); return
    else:
        d = wait_for_signal()
        if d == 0: log("window closing before a signal -> staying flat."); return
        if PAPER:
            si = info(SYMBOL); cz = si.trade_contract_size; px = mid(SYMBOL)
            tlots = TARGET_LEV * start_eq / (cz * px)
            log(f"[PAPER] would {'SHORT' if d<0 else 'LONG'} stack to ~{tlots:.0f} lots ({TARGET_LEV:.0f}x) @ ~{px:.2f}")
            log(f"[PAPER] then LOCK on proj-score>={LOCK_SCORE} / STOP eq<={STOP_EQ:,.0f}. dry-run only; re-run with 'live'."); return
        p = stack_to_target(SYMBOL, d, start_eq)
        if p is None: log("no fills -> no position. exiting (re-run to retry)."); return
        px = mid(SYMBOL); si = info(SYMBOL)
        lev = p.volume * si.trade_contract_size * px / mt5.account_info().equity
        log(f"ACHIEVED: {'LONG' if p.type==mt5.POSITION_TYPE_BUY else 'SHORT'} {p.volume:.2f} lots @ {p.price_open:.2f} = {lev:.2f}x"
            f"{'  <<< capped below target by liquidity' if lev < 0.8*TARGET_LEV else ''}")

    set_broker_sl(SYMBOL, p)
    manage(SYMBOL)

if __name__ == "__main__":
    import sys
    if "flatten" in sys.argv:
        MODE = "live"; ARMED = True; PAPER = False
        need_mt5(); flatten(SYMBOL, "manual-flatten"); raise SystemExit
    if "live" in sys.argv:
        MODE = "live"; ARMED = True; PAPER = False
        print(">>> LIVE MODE ARMED (final-day) <<<")
    main()
