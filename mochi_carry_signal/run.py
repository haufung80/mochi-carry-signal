"""Entrypoint: run the FastAPI app under uvicorn.

    python run.py
    # or, after `pip install -e .`:
    mochi-carry-signal
    # or directly:
    uvicorn mochi_carry_signal.web:app --reload
"""
from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8100"))
    uvicorn.run("mochi_carry_signal.web:app", host=host, port=port,
                reload=bool(os.environ.get("RELOAD")))


if __name__ == "__main__":
    main()
