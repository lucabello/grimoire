"""CLI entrypoint for Grimoire."""

import argparse
import os
import sys
from pathlib import Path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="grimoire", description="Run the Grimoire server.")
    parser.add_argument(
        "--config-file",
        type=Path,
        help="path to the config file (default: $GRIMOIRE_CONFIG or ./config.yaml)",
    )
    parser.add_argument("--port", type=int, default=8000, help="port to listen on (default: 8000)")
    parser.add_argument(
        "--host", default="0.0.0.0", help="interface to bind to (default: 0.0.0.0)"
    )
    args = parser.parse_args(argv)
    if args.config_file is not None and not args.config_file.is_file():
        parser.error(f"config file not found: {args.config_file}")
    return args


def main(argv: list[str] | None = None) -> None:
    """Run the Grimoire server."""
    import uvicorn

    args = _parse_args(argv)
    if args.config_file is not None:
        # The app factory resolves its config (and save-weights target) from this variable.
        os.environ["GRIMOIRE_CONFIG"] = str(args.config_file)

    uvicorn.run(
        "grimoire.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        loop="asyncio",
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
