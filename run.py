#!/usr/bin/env python
"""Convenience launcher so `python run.py` works from the repo root.

Delegates to the packaged entrypoint. Env: HOST (127.0.0.1), PORT (8100),
RELOAD (set to enable auto-reload).
"""
from mochi_carry_signal.run import main

if __name__ == "__main__":
    main()
