import sqlite3
from pathlib import Path

# Database and output paths
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(r"C:\Users\limli\Inti Folder\FYP\machine_monitor.db")
OUTPUT_PATH = BASE_DIR / "database_schema.md"


def main():
    # Check whether the database exists
    if not DB_PATH.exists():
        print(f"Error: Database not found: {DB_PATH}")
        print("Please check the database path before running this script.")
        return

    conn = sqlite3.connect(DB_PATH)

    try:
        with conn:
            # Get all user tables
            tables = conn.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name;
            """).fetchall()

            with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                f.write("# Database Schema\n\n")
                f.write(f"- **Database:** `{DB_PATH.name}`\n")
                f.write(f"- **Tables:** {len(tables)}\n\n")

                if not tables:
                    f.write("No user tables found in this database.\n")

                for (table_name,) in tables:
                    # Escape backticks in Markdown table names
                    safe_table_name = table_name.replace("`", "\\`")

                    f.write(f"## `{safe_table_name}`\n\n")

                    # Get column information
                    columns = conn.execute(
                        f'PRAGMA table_info("{table_name.replace(chr(34), chr(34) * 2)}")'
                    ).fetchall()

                    f.write(
                        "| Column | Type | Not Null | Default | "
                        "Primary Key | Primary Key Order |\n"
                    )
                    f.write(
                        "|---|---|---|---|---|---|\n"
                    )

                    for col in columns:
                        cid, name, col_type, notnull, default, pk = col

                        # Display NULL defaults clearly
                        default_value = (
                            "NULL" if default is None else str(default)
                        )

                        # Escape Markdown table delimiters
                        safe_name = str(name).replace("|", "\\|")
                        safe_type = str(col_type or "").replace("|", "\\|")
                        safe_default = default_value.replace("|", "\\|")

                        f.write(
                            f"| `{safe_name}` | `{safe_type}` | "
                            f"{'Yes' if notnull else 'No'} | "
                            f"`{safe_default}` | "
                            f"{'Yes' if pk else 'No'} | {pk} |\n"
                        )

                    # Get foreign key information
                    foreign_keys = conn.execute(
                        f'PRAGMA foreign_key_list("{table_name.replace(chr(34), chr(34) * 2)}")'
                    ).fetchall()

                    f.write("\n### Foreign Keys\n\n")

                    if foreign_keys:
                        f.write(
                            "| Column | References Table | "
                            "References Column | On Update | On Delete |\n"
                        )
                        f.write(
                            "|---|---|---|---|---|\n"
                        )

                        for fk in foreign_keys:
                            # PRAGMA foreign_key_list fields:
                            # id, seq, table, from, to, on_update,
                            # on_delete, match
                            _, _, ref_table, from_col, to_col, on_update, on_delete, _ = fk

                            f.write(
                                f"| `{from_col}` | `{ref_table}` | "
                                f"`{to_col or 'Primary Key'}` | "
                                f"{on_update} | {on_delete} |\n"
                            )
                    else:
                        f.write("None declared.\n")

                    # Get indexes
                    indexes = conn.execute(
                        f'PRAGMA index_list("{table_name.replace(chr(34), chr(34) * 2)}")'
                    ).fetchall()

                    f.write("\n### Indexes\n\n")

                    if indexes:
                        f.write(
                            "| Index Name | Unique | Origin | Partial |\n"
                        )
                        f.write("|---|---|---|---|\n")

                        for index in indexes:
                            # PRAGMA index_list fields:
                            # seq, name, unique, origin, partial
                            _, index_name, unique, origin, partial = index

                            f.write(
                                f"| `{index_name}` | "
                                f"{'Yes' if unique else 'No'} | "
                                f"{origin} | "
                                f"{'Yes' if partial else 'No'} |\n"
                            )

                            # Get indexed columns
                            index_columns = conn.execute(
                                f'PRAGMA index_info("{index_name.replace(chr(34), chr(34) * 2)}")'
                            ).fetchall()

                            indexed_columns = [
                                item[2] for item in index_columns
                            ]

                            f.write(
                                f"\nIndexed columns: "
                                f"{', '.join(f'`{c}`' for c in indexed_columns)}\n"
                            )
                    else:
                        f.write("None declared.\n")

                    # Get row count
                    quoted_table = (
                        '"' + table_name.replace('"', '""') + '"'
                    )
                    row_count = conn.execute(
                        f"SELECT COUNT(*) FROM {quoted_table}"
                    ).fetchone()[0]

                    f.write(f"\n**Row count:** {row_count}\n\n")
                    f.write("---\n\n")

        print("Database schema exported successfully.")
        print(f"Database: {DB_PATH}")
        print(f"Output:   {OUTPUT_PATH}")

    except sqlite3.Error as error:
        print(f"SQLite error: {error}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()