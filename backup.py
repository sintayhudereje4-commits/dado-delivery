#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backup for Dado Delivery.

Usage:
    python backup.py                 # writes backups/dado_backup_<timestamp>.zip (or .sql.gz)
    python backup.py --keep 14       # keep only the 14 newest backups (default 14)
    python backup.py --out /path     # choose the backup folder

* PostgreSQL (DATABASE_URL set): runs `pg_dump` (must be installed). Because uploaded
  files are also stored in the database (table stored_file), the dump contains everything.
* SQLite: makes a consistent copy with SQLite's backup API and zips it together with
  static/uploads and instance/private_uploads.

Schedule it (cron example, every night at 02:00):
    0 2 * * * cd /path/to/dado_delivery && python backup.py >> backup.log 2>&1
Copy the resulting files OFF the server (another machine / cloud storage).
"""
import argparse
import glob
import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))


def pg_backup(url, out_dir, stamp):
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    if not shutil.which('pg_dump'):
        sys.exit('pg_dump not found. Install the PostgreSQL client tools, or use your host\'s built-in backups.')
    target = os.path.join(out_dir, f'dado_backup_{stamp}.sql.gz')
    proc = subprocess.Popen(['pg_dump', '--no-owner', '--no-privileges', url],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with gzip.open(target, 'wb') as gz:
        shutil.copyfileobj(proc.stdout, gz)
    err = proc.stderr.read().decode('utf-8', 'ignore')
    if proc.wait() != 0:
        os.remove(target)
        sys.exit('pg_dump failed: ' + err)
    return target


def sqlite_backup(out_dir, stamp):
    db_path = os.path.join(ROOT, 'dado.db')
    if not os.path.isfile(db_path):
        sys.exit(f'Database not found: {db_path}')
    target = os.path.join(out_dir, f'dado_backup_{stamp}.zip')
    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, 'dado.db')
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, 'dado.db')
            for folder, arc in ((os.path.join(ROOT, 'static', 'uploads'), 'static/uploads'),
                                (os.path.join(ROOT, 'instance', 'private_uploads'), 'instance/private_uploads')):
                if os.path.isdir(folder):
                    for name in os.listdir(folder):
                        full = os.path.join(folder, name)
                        if os.path.isfile(full):
                            zf.write(full, f'{arc}/{name}')
    return target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(ROOT, 'backups'))
    ap.add_argument('--keep', type=int, default=14)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    url = os.environ.get('DATABASE_URL', '').strip()
    target = pg_backup(url, args.out, stamp) if url else sqlite_backup(args.out, stamp)
    print('Backup written:', target, f'({os.path.getsize(target)//1024} KB)')
    files = sorted(glob.glob(os.path.join(args.out, 'dado_backup_*')))
    for old in files[:-max(1, args.keep)]:
        os.remove(old)
        print('Removed old backup:', old)


if __name__ == '__main__':
    main()
