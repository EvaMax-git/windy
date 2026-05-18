"""Allow ``python -m mneme.worker`` to run the worker process.

This is the entry-point used by docker-compose.yaml.
"""

from mneme.worker.app import main

if __name__ == "__main__":
    main()
