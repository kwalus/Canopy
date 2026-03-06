#!/bin/sh
# Canopy container entrypoint
# Runs once at container start (after volumes are mounted) to initialise the
# database, then hands off to the main application process.
set -e

echo "Canopy: initialising database..."
python -c "
from canopy.core.app import create_app
from canopy.core.config import Config
create_app(Config.from_env())
"
echo "Canopy: database ready."

# Replace this shell with the Canopy process so Docker signals are forwarded
# correctly (SIGTERM for graceful shutdown).
exec python -m canopy.main "$@"
