import sqlite3
import os

db_path = "machine_monitor.db"

conn = sqlite3.connect(db_path)

tables = conn.execute("""
    SELECT name
    FROM sqlite_master
    WHERE type='table'
    ORDER BY name;
""").fetchall()

with open("database_schema.md", "w", encoding="utf-8") as f:

    f.write("# Database Schema\n\n")
    f.write("Database: `machine_monitor.db`\n\n")

    for (table,) in tables:

        f.write(f"## `{table}`\n\n")

        columns = conn.execute(
            f"PRAGMA table_info('{table}')"
        ).fetchall()

        f.write("| Column | Type | Not Null | Default | Primary Key |\n")
        f.write("|---|---|---|---|---|\n")

        for col in columns:
            cid, name, col_type, notnull, default, pk = col

            f.write(
                f"| `{name}` | `{col_type}` | "
                f"{notnull} | `{default}` | {pk} |\n"
            )

        count = conn.execute(
            f"SELECT COUNT(*) FROM `{table}`"
        ).fetchone()[0]

        f.write(f"\n**Row count:** {count}\n\n")

conn.close()

print("database_schema.md created")
print("Location:", os.path.abspath("database_schema.md"))