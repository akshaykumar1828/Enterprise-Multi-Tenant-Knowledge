"""One scheduled database backup: timestamped dump, archive check, retention, log line.

    .venv\\Scripts\\python.exe -m src.rag.backup_schedule --dir C:\\rag-backups --keep 14 --log-dir C:\\rag-logs

Run by deploy\\run_backup_task.ps1 from the "RAG Backup" scheduled task (see
deploy\\BACKUP.md). It reuses src.rag.backup: create_backup() runs pg_dump (custom
format, read-only for the database) and then pg_restore --list, and refuses to
overwrite an existing file. A backup counts as successful only if that archive check
passes; an unreadable archive is renamed to <name>.invalid (so it never counts as a
backup) and the run fails.

Retention runs only after a successful backup. It considers nothing but regular files
directly in the backup folder whose names match <database>-YYYYMMDD-HHMMSS.dump
exactly, keeps the newest --keep of them, and never removes the file just created.
Every other file (other databases' dumps, .invalid files, notes, folders) is left alone.

Connection settings and the password come from the environment (PG*, a pgpass file
via PGPASSFILE, or .env); nothing here prints or logs them. Exit codes: 0 success,
1 backup failed (nothing pruned), 2 backup succeeded but retention failed.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import backup

DEFAULT_KEEP = 14  # one per day for two weeks
LOG_FILE = "backup.log"


# --- names ---------------------------------------------------------------------------------

def _safe_database(database: str) -> str:
    safe = re.sub(r"[^a-z0-9_]", "_", database.lower())[:40]
    if not safe.strip("_"):
        raise backup.BackupError("PGDATABASE does not give a usable backup name")
    return safe


def backup_name(database: str, when: datetime) -> str:
    """<database>-YYYYMMDD-HHMMSS.dump; only [a-z0-9_-.], never a path."""
    return f"{_safe_database(database)}-{when:%Y%m%d-%H%M%S}.dump"


def backup_pattern(database: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(_safe_database(database))}-\d{{8}}-\d{{6}}\.dump$")


def matching_backups(directory: Path, database: str) -> list[Path]:
    """This database's backups in `directory` (not recursive), oldest first.

    Only regular files (no folders, no links) whose names match the pattern exactly.
    The timestamp in the name decides the order, not the file time.
    """
    directory = Path(directory).resolve()
    pattern = backup_pattern(database)
    found = [p for p in directory.iterdir()
             if pattern.fullmatch(p.name) and p.is_file() and not p.is_symlink() and p.resolve().parent == directory]
    return sorted(found, key=lambda p: p.name)


@dataclass
class RetentionResult:
    kept: list[Path]
    removed: list[Path]


def apply_retention(directory: Path, database: str, keep: int, protect: Path) -> RetentionResult:
    """Delete all but the newest `keep` matching backups; `protect` (the new backup) always stays."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    protect = Path(protect).resolve()
    backups = matching_backups(directory, database)
    newest_first = list(reversed(backups))
    keep_set = {p.resolve() for p in newest_first[:keep]} | {protect}
    removed = []
    for path in backups:
        if path.resolve() not in keep_set:
            path.unlink()
            removed.append(path)
    kept = [p for p in backups if p not in removed]
    return RetentionResult(kept, removed)


# --- logging without secrets ------------------------------------------------------------------

_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*\S+")


def sanitize(text: str) -> str:
    """Mask the values of secret settings in the environment and anything shaped like password=..."""
    for name in backup.SECRET_SETTINGS:
        value = os.environ.get(name, "")
        if len(value) >= 4:
            text = text.replace(value, "***")
    return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=***", text)


def write_log(log_dir: Path, message: str) -> str:
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {sanitize(message)}"
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / LOG_FILE).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return line


# --- one run ------------------------------------------------------------------------------------

def run(directory: Path, keep: int, log_dir: Path, now: datetime | None = None) -> int:
    """Create, check and prune. Returns the exit code (0 ok, 1 backup failed, 2 retention failed)."""
    def log(message: str) -> None:
        print(write_log(log_dir, message))

    try:
        if keep < 1:
            raise ValueError("--keep must be at least 1")
        database = backup.app_database()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / backup_name(database, now or datetime.now())
    except (ValueError, OSError, backup.BackupError) as error:
        log(f"FAILED backup: {error.__class__.__name__}: {error}")
        return 1

    if out.exists():  # never overwrite, and never touch, an existing file
        log(f"FAILED backup {out.name}: a file with this name already exists; it was left unchanged")
        return 1
    try:
        info = backup.create_backup(out)  # pg_dump, then pg_restore --list must list every application table
    except (backup.BackupError, OSError) as error:
        if out.exists():  # an unreadable archive must never count as a backup
            invalid = out.with_name(out.name + ".invalid")
            out.replace(invalid)
            log(f"FAILED backup {out.name}: archive rejected, kept for inspection as {invalid.name}")
        log(f"FAILED backup {out.name}: {error.__class__.__name__}: {error}")
        return 1
    log(f"OK backup {info.path.name} size={info.size_bytes} sha256={info.sha256} "
        f"tables={len(info.tables_with_data)}")

    try:
        result = apply_retention(directory, database, keep, protect=info.path)
    except (ValueError, OSError) as error:
        log(f"FAILED retention in {directory}: {error.__class__.__name__}: {error}")
        return 2
    log(f"OK retention keep={keep}: retained={len(result.kept)} removed={len(result.removed)}"
        + (f" ({', '.join(p.name for p in result.removed)})" if result.removed else ""))
    return 0


def main() -> int:
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="One scheduled backup of PGDATABASE: dump, check, retention.")
    parser.add_argument("--dir", type=Path, required=True, help="backup folder (outside the repository)")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"backups to keep (default {DEFAULT_KEEP})")
    parser.add_argument("--log-dir", type=Path, required=True, help=f"folder for {LOG_FILE}")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # does not override PGUSER/PGPASSFILE set by the task
    return run(args.dir, args.keep, args.log_dir)


if __name__ == "__main__":
    sys.exit(main())
