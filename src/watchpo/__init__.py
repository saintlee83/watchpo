"""watchpo: an AI watchdog that tracks upstream open-source changes and suggests
improvements to your own product."""

__version__ = "0.1.0"


def main() -> None:
    import sys

    from .cli import main as _main

    sys.exit(_main())
