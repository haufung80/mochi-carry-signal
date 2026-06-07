"""mochi-carry-signal — funding-arb SIGNAL GENERATOR.

Watch live Hyperliquid funding for BTC/ETH/SOL, compute a trailing
cash-and-carry carry signal (LOCKED: 72h trailing per-hour funding average,
OPEN when avg >= 10%/yr and HL spot exists, CLOSE when avg <= 0), RECORD each
state transition, show a dashboard, and on the USER's approval fire an order to
the position-manager's funding-arb API. It NEVER fires automatically —
approve-to-fire only.
"""

__version__ = "0.1.0"
