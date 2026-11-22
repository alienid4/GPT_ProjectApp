"""Read-only operational observations; never return business identifiers or paths."""
import os
import re
import shutil
from pathlib import Path

from flask import current_app

from .models import utcnow


def status_snapshot():
    result = {'at': utcnow().isoformat() + 'Z', 'mode': current_app.config['APP_MODE'],
              'database': current_app.extensions['engine'].dialect.name,
              'storage': {'status': 'unavailable', 'free_bytes': None, 'total_bytes': None},
              'upload_checks': {'status': 'unknown', 'count': None, 'limited': False}}
    try:
        root = Path(current_app.config['STORAGE_PATH']).resolve()
        usage = shutil.disk_usage(root)
        if not root.is_dir():
            return result
        result['storage'] = {'status': 'readable', 'free_bytes': usage.free, 'total_bytes': usage.total}
        journal = root / '.pending-uploads'
        # No follow-through into a substituted directory, and no filenames leave this function.
        if journal.is_symlink():
            return result
        count = scanned = 0
        if journal.exists():
            with os.scandir(journal) as entries:
                for entry in entries:
                    scanned += 1
                    if re.fullmatch('[0-9a-f]{64}', entry.name) and entry.is_file(follow_symlinks=False):
                        count += 1
                    if scanned >= 10000:
                        result['upload_checks'] = {'status': 'observed', 'count': count, 'limited': True}
                        return result
        result['upload_checks'] = {'status': 'observed', 'count': count, 'limited': False}
    except OSError:
        # Unavailable is deliberately not represented as zero pending work.
        pass
    return result
