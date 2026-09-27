"""Allow `python -m analytics.worker`."""

from .worker import main

raise SystemExit(main())
