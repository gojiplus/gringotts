"""Quick example demonstrating CRUD helpers."""

import sqlite3

from crud import create_user, delete_user, read_user, update_user_credits


def main() -> None:
    db_path = "example.db"

    # Ensure the User table exists.
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS User (
            Username TEXT PRIMARY KEY,
            Password TEXT,
            Email TEXT,
            APIKey TEXT UNIQUE,
            TotalRemainingCredits INTEGER
        )
        """
    )
    conn.commit()
    conn.close()

    # Demonstrate the helper functions.
    create_user("alice", "password", "alice@example.com", "key123", 100, db_path=db_path)
    print("After creation:", read_user("alice", db_path=db_path))
    update_user_credits("alice", 150, db_path=db_path)
    print("After credit update:", read_user("alice", db_path=db_path))
    delete_user("alice", db_path=db_path)
    print("After deletion:", read_user("alice", db_path=db_path))


if __name__ == "__main__":
    main()

