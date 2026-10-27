from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from .config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stop local oopz-mcp output without a model/MCP call"
    )
    parser.add_argument(
        "--config", type=Path, required=True, help="same config as the running server"
    )
    args = parser.parse_args()
    target = Path(load_settings(args.config).local_stop_file)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        output.write(str(time.time_ns()))
    print("Local stop requested; remote audio tail and native inference are not confirmed stopped.")


if __name__ == "__main__":
    main()
