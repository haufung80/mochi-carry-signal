"""SQLAlchemy models — just the `Signal` record.

One row per RECORDED state transition (FLAT->OPEN => kind=OPEN, OPEN->CLOSE =>
kind=CLOSE). The ``idempotency_key`` is UNIQUE and deterministic per
(asset, transition, hour); it is ALSO the key sent to the PM's
``/funding-arb/open`` so the PM's dedup aligns with ours.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Lifecycle of a recorded signal.
#   pending  — recorded, awaiting the user's approve/reject
#   approved — approve accepted, fire about to be attempted (transient)
#   fired    — order successfully sent to the PM (arb_id stored for OPENs)
#   rejected — user rejected, or approve failed auth
#   error    — fire was attempted but the PM call failed (error_message set)
SIGNAL_STATUSES = ("pending", "approved", "fired", "rejected", "error")


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_signals_idemp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    asset: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)   # OPEN | CLOSE

    # Signal snapshot at the moment of recording (for the dashboard + audit).
    trailing_avg_pph: Mapped[float] = mapped_column(Float, nullable=False)
    trailing_avg_apr: Mapped[float] = mapped_column(Float, nullable=False)
    funding_now_apr: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_available: Mapped[bool] = mapped_column(default=False, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True)
    # Deterministic per (asset, transition, hour); ALSO sent to the PM /open.
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    # Filled once fired. For an OPEN, the PM's arb_id; for a CLOSE, the arb_id
    # we closed (copied from the matching open signal/arb).
    arb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
