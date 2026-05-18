"""Allow ``python -m mneme.backup`` to invoke the CLI."""

import sys

from mneme.backup.cli import main

sys.exit(main())
