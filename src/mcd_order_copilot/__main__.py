"""Allow ``python -m mcd_order_copilot``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
