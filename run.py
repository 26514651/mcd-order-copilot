#!/usr/bin/env python3
"""Zero-install entry point.

``python -m mcd_order_copilot`` only works after ``pip install -e .`` (or with
``PYTHONPATH=src``). This file exists so that a reviewer who has just cloned the
repository can run it immediately:

    python run.py --demo
    python run.py --demo --goal cut --weight 70
    python run.py --coverage

The same code is reachable as an installed console script (``mcd-order-copilot``)
once the package is installed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from mcd_order_copilot.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
