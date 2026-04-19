from __future__ import annotations

import sys

from email_intel.scheduler import run_forever


def main() -> int:
    return run_forever()


if __name__ == "__main__":
    sys.exit(main())
