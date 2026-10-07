"""Entry point for `python -m pdfind` (used by the search bar's re-exec)."""

import sys

from .cli import main

sys.exit(main())
