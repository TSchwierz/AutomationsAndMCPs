from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow `python -m studio_climate` when run from the pi/ directory.
_PI_ROOT = Path(__file__).resolve().parent.parent
if str(_PI_ROOT) not in sys.path:
    sys.path.insert(0, str(_PI_ROOT))

from studio_climate.api import run_api
from studio_climate.collector import run_collector
from studio_climate.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Studio Climate Monitor")
    parser.add_argument(
        "command",
        choices=("collector", "api", "all"),
        help="collector=sensor loop; api=LAN FastAPI; all=both (dev helper)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.toml (defaults to pi/config.toml or config.example.toml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)

    if args.command == "collector":
        run_collector(cfg)
    elif args.command == "api":
        run_api(cfg)
    else:
        import threading

        thread = threading.Thread(target=run_collector, args=(cfg,), daemon=True)
        thread.start()
        run_api(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
