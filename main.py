"""Entry point: python main.py [--dry-run] [--source NAME] [--config PATH] [-v]"""

import sys

from jobbot.app import main

if __name__ == "__main__":
    sys.exit(main())
