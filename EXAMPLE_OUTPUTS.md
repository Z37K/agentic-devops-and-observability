# Example outputs

Representative output from each runnable component, so the behaviour is clear without
running anything. All captured from real runs on real data / live endpoints.

---

## Live MT5 execution — `execution/mt5_execute_finals.py`

Real extracts from a live finals run.

**(a) Selective entry gate — holding fire on a weak signal.** Entry requires a strong 12h
momentum (|mom| ≥ 1.50%). Here momentum sits around 1.0%, below the gate, so it does **not**
trade — the discipline of the selective strong-signal entry, live:

```
[14:10:30Z]   12h mom=+1.13% (need |.|>=1.50%)
[14:10:35Z]   12h mom=+1.06% (need |.|>=1.50%)
[14:10:45Z]   12h mom=+0.99% (need |.|>=1.50%)
[14:10:55Z]   12h mom=+0.98% (need |.|>=1.50%)
```

**(b) Signal fires → stack toward 8x → cap cleanly on thin liquidity.** When momentum crosses
the gate it enters and stacks toward the 8x target, handling partial fills and stopping
cleanly when liquidity runs out (the exact partial-fill robustness the Nemotron sentinel
checks for), then arms a server-side breakeven stop:

```
[14:12:05Z] SIGNAL FIRED: 12h mom=+1.76% -> LONG
[14:12:05Z] stacking toward 8x = 133.32 lots LONG ...
[14:12:05Z]   order 100.00 lots (have 0.00/133.32)
[14:12:06Z]   -> 47.00 lots
[14:12:14Z]   order 86.32 lots (have 47.00/133.32)
[14:12:14Z]   -> 102.00 lots
[14:12:22Z]   order 31.31 lots (have 102.00/133.32)
[14:12:28Z]   no fill (1/3) - thin liquidity
[14:12:42Z]   no fill (2/3) - thin liquidity
[14:12:56Z]   no fill (3/3) - thin liquidity
[14:12:56Z]   stopping stack: liquidity exhausted
[14:12:56Z] ACHIEVED: LONG 102.00 lots @ 59975.22 = 6.11x  <<< capped below target by liquidity
[14:12:56Z] broker SL @ 57535.16  (= breakeven floor; server-side DQ/blowup guard)
```

**(c) Live position management.** Once in a position it tracks price, equity, effective
leverage, an estimated 15-minute-equity Sharpe, and a projected leaderboard score against the
lock threshold (≈ 80.6, the Top-25 line):

```
[14:23:16Z] px=60012.39 eq=1,001,971 (+0.20%) lev=6.11x est_shp=0.110 -> PROJ SCORE 54.90 (lock>=80.6)
[14:23:31Z] px=60010.43 eq=1,002,202 (+0.22%) lev=6.11x est_shp=0.110 -> PROJ SCORE 55.60 (lock>=80.6)
[14:23:36Z] px=59995.29 eq=1,000,616 (+0.06%) lev=6.12x est_shp=0.110 -> PROJ SCORE 50.70 (lock>=80.6)
[14:23:46Z] px=59988.80 eq=1,000,031 (+0.00%) lev=6.12x est_shp=0.110 -> PROJ SCORE 45.80 (lock>=80.6)
```

Effective leverage holds ~6x (within the 8x target, far under the 28x/30x limits), and the
projected-score-vs-lock logic is what drives the optional-stopping exit.

---

## Independent code review — `code-review/ai_code_review.py`

A real call to `nvidia/llama-3.3-nemotron-super-49b-v1.5` via NVIDIA NIM (hosted, no GPU). An
independent model reviews the live execution script for the bug class that fast LLM codegen
introduces. Reviewing `execution/mt5_execute_finals.py`:

```
### Code Review: mt5_execute_finals.py — highest-risk first

1. Orders sent but fill never confirmed (assumed successful)
   In stack_to_target, after send_add the code does not verify the fill actually occurred;
   a failed/partial fill can lead to over-leveraging or wrong sizing.
   Fix: confirm volume increased before continuing the stack.

2. Position left open during flatten (partial fills)
   If the position can't be fully closed within the 180s timeout, flatten exits without
   ensuring it is closed — residual lots could survive HARD_EXIT.
   Fix: loop until flat or HARD_EXIT.

3. Missing error handling on broker calls (order_send retcode not acted on in flatten).
4. volume/lot math: guard volume_step against zero.
5. Race condition note on TARGET_EQ / STOP_EQ / HARD_EXIT ordering in manage().

No critical issues in: Sharpe logging, mid()/info(), set_broker_sl (guarded), mom12.
Summary: prioritise unconfirmed-fills and incomplete-flatten first.
```

Note: items 1 and 2 are exactly the failure modes that matter most in a live executor.

---

## Deterministic state check — `state-checks/account_state_check.py`

Dumb, reliable checks on the live account state, to catch coding-error symptoms so the
operator can intervene. Reports only; never trades. Demo on a sample state with seeded errors:

```
[CRITICAL] 1 order(s) sent without a confirmed fill - do not assume they executed.
[CRITICAL] Position direction buy != intended sell on BTCUSD.
[WARN]     Leverage 11.5x overshoots target 8.0x by >25% - check the stacking loop.
```
