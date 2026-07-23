"""Allow ``python -m railpulse <command>``."""

from .cli import main

raise SystemExit(main())
