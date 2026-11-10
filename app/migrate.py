"""CLI: apply db/migrations/*.sql against DATABASE_URL (idempotent, in order)."""

from __future__ import annotations

from app import db
from app.config import settings


def main() -> None:
    conn = db.connect(settings.database_url)
    try:
        applied = db.run_migrations(conn)
    finally:
        conn.close()
    if applied:
        print("applied:", ", ".join(applied))
    else:
        print("migrations up to date")


if __name__ == "__main__":
    main()
