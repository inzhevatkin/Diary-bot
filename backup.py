import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from storage import DIARY_DB_PATH
from storage import initialize_storage


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_BACKUP_DIR = BASE_DIR / "data" / "backups" / "sqlite"
BACKUP_PREFIX = "diary_"
BACKUP_SUFFIX = ".sqlite3"


def create_sqlite_backup(
    source_path: Path = DIARY_DB_PATH,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    keep: int = 14,
) -> Path:
    source_path = source_path.resolve()
    if source_path == DIARY_DB_PATH.resolve():
        initialize_storage()
    if not source_path.exists():
        raise FileNotFoundError(f"Database does not exist: {source_path}")

    backup_dir = backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{BACKUP_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{BACKUP_SUFFIX}"

    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)

    verify_backup(backup_path)
    prune_old_backups(backup_dir, keep)
    return backup_path


def verify_backup(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"Backup integrity check failed for {path}: {result}")


def prune_old_backups(backup_dir: Path, keep: int) -> list[Path]:
    if keep < 1:
        return []

    backups = sorted(
        backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    deleted: list[Path] = []
    for path in backups[keep:]:
        path.unlink()
        deleted.append(path)
    return deleted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a SQLite backup of the diary database.")
    parser.add_argument("--source", type=Path, default=DIARY_DB_PATH, help="SQLite database path.")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help="Directory for backups.")
    parser.add_argument("--keep", type=int, default=14, help="How many latest backups to keep.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    backup_path = create_sqlite_backup(args.source, args.backup_dir, args.keep)
    print(f"Created backup: {backup_path}")


if __name__ == "__main__":
    main()
