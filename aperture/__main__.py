"""Allows ``python3 -m aperture``."""

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
