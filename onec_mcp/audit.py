"""Durable, payload-free write journal shared by local connector processes."""

import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path


class AuditError(ValueError):
    """The journal could not durably record the transition."""


def _actor():
    if os.name == "nt":
        return os.getlogin()
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def _now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class AuditLog:
    def __init__(self, path):
        # Resolve ':memory:' as a filename, never SQLite's ephemeral special database.
        self.path = Path(path).absolute()

    def lookup(self, operation_id):
        try:
            if not self.path.exists():
                return None
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    """SELECT operation_id, started_at, finished_at, actor, ib_user,
                       base, action, collection, object_id, outcome, http_status
                       FROM operations WHERE operation_id=?""",
                    (operation_id,),
                ).fetchone()
                return dict(row) if row is not None else None
        except (sqlite3.Error, OSError, UnicodeError) as exc:
            raise AuditError("Не удалось прочитать журнал изменений.") from exc

    @contextmanager
    def _transaction(self):
        try:
            # SQLite itself creates new DB files using process umask. Pre-create
            # ours with owner-only permissions on POSIX without touching the umask.
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            with closing(sqlite3.connect(self.path, timeout=2)) as db:
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    yield db
        except (sqlite3.Error, OSError, KeyError, UnicodeError) as exc:
            raise AuditError("Не удалось записать журнал изменений.") from exc

    def start(self, operation_id, base, action, collection, object_id, ib_user):
        with self._transaction() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT, actor TEXT NOT NULL, ib_user TEXT,
                    base TEXT NOT NULL, action TEXT NOT NULL, collection TEXT NOT NULL,
                    object_id TEXT, outcome TEXT NOT NULL, http_status INTEGER
                )"""
            )
            db.execute(
                """INSERT INTO operations
                (operation_id, started_at, actor, ib_user, base, action, collection, object_id, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'started')""",
                (operation_id, _now(), _actor(), ib_user, base, action, collection, object_id),
            )

    def finish(self, operation_id, outcome, *, object_id=None, status=None):
        with self._transaction() as db:
            updated = db.execute(
                """UPDATE operations SET finished_at=?, outcome=?,
                    object_id=COALESCE(?, object_id), http_status=?
                    WHERE operation_id=? AND outcome='started'""",
                (_now(), outcome, object_id, status, operation_id),
            )
            if updated.rowcount != 1:
                raise AuditError("Исходная запись журнала отсутствует или уже завершена.")
