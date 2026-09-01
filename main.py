"""Script entry point for the Italian Street Extractor.

Delegates to :func:`strade.cli.main` so that ``python main.py extract ...``
behaves identically to the installed ``strade`` console script.
"""

import sys

from strade.cli import main

if __name__ == "__main__":
    sys.exit(main())
