import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BACKUPS_DIR = DATA_DIR / "backups"
DIARY_JSONL_PATH = DATA_DIR / "diary.jsonl"
DIARY_DB_PATH = DATA_DIR / "diary.sqlite3"


def initialize_storage() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    BACKUPS_DIR.mkdir(exist_ok=True)
    ensure_database()
    migrate_jsonl_if_needed()


def connect(db_path: Path = DIARY_DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def ensure_database(db_path: Path = DIARY_DB_PATH) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS diary_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                diary_date TEXT,
                entry_type TEXT,
                message_sent_at TEXT,
                created_at TEXT,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_diary_entries_chat_date ON diary_entries(chat_id, diary_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_diary_entries_date ON diary_entries(diary_date)"
        )


def migrate_jsonl_if_needed(
    jsonl_path: Path = DIARY_JSONL_PATH,
    db_path: Path = DIARY_DB_PATH,
) -> int:
    if not jsonl_path.exists():
        return 0

    ensure_database(db_path)
    with connect(db_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM diary_entries").fetchone()[0]
        if row_count:
            return 0

        entries = read_entries_from_jsonl(jsonl_path)
        insert_entries(connection, entries)
        if entries:
            logging.info("Migrated %s diary entries from %s to %s.", len(entries), jsonl_path, db_path)
        return len(entries)


def read_entries_from_jsonl(path: Path = DIARY_JSONL_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logging.warning("Skipping invalid diary line: %s", line[:120])
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def append_entry(entry: dict[str, Any], db_path: Path = DIARY_DB_PATH) -> None:
    ensure_database(db_path)
    with connect(db_path) as connection:
        insert_entries(connection, [entry])


def read_entries(chat_id: int | None = None, db_path: Path = DIARY_DB_PATH) -> list[dict[str, Any]]:
    ensure_database(db_path)
    migrate_jsonl_if_needed(db_path=db_path)
    with connect(db_path) as connection:
        if chat_id is None:
            rows = connection.execute(
                "SELECT payload_json FROM diary_entries ORDER BY id"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT payload_json FROM diary_entries WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()

    entries: list[dict[str, Any]] = []
    for row in rows:
        try:
            entry = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def rewrite_entries_with_backup(
    entries: list[dict[str, Any]],
    db_path: Path = DIARY_DB_PATH,
    backup_dir: Path = BACKUPS_DIR,
) -> Path:
    ensure_database(db_path)
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / f"diary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    current_entries = read_entries(db_path=db_path)
    write_entries_jsonl(current_entries, backup_path)

    with connect(db_path) as connection:
        connection.execute("DELETE FROM diary_entries")
        insert_entries(connection, entries)
    return backup_path


def export_jsonl(path: Path = DIARY_JSONL_PATH, db_path: Path = DIARY_DB_PATH) -> Path:
    entries = read_entries(db_path=db_path)
    write_entries_jsonl(entries, path)
    return path


def insert_entries(connection: sqlite3.Connection, entries: list[dict[str, Any]]) -> None:
    connection.executemany(
        """
        INSERT INTO diary_entries (
            chat_id,
            user_id,
            diary_date,
            entry_type,
            message_sent_at,
            created_at,
            payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [entry_record(entry) for entry in entries],
    )


def entry_record(entry: dict[str, Any]) -> tuple[Any, Any, Any, Any, Any, Any, str]:
    chat = entry.get("chat") if isinstance(entry.get("chat"), dict) else {}
    user = entry.get("user") if isinstance(entry.get("user"), dict) else {}
    return (
        chat.get("id"),
        user.get("id"),
        entry.get("diary_date") or entry.get("message_sent_date"),
        entry.get("type"),
        entry.get("message_sent_at"),
        entry.get("created_at"),
        json.dumps(entry, ensure_ascii=False),
    )


def write_entries_jsonl(entries: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(exist_ok=True)
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


if __name__ == "__main__":
    initialize_storage()
    exported_path = export_jsonl()
    print(f"Storage is ready. JSONL export: {exported_path}")
