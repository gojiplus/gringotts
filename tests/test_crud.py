import sqlite3
import sys
from pathlib import Path


# Make the crud module importable
sys.path.append(str(Path(__file__).resolve().parents[1] / "gringotts"))

from crud import create_user, delete_user, read_user, update_user_credits


def setup_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE User (
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


def test_crud_cycle(tmp_path):
    db_path = tmp_path / "test.db"
    setup_db(db_path)

    create_user("bob", "pw", "bob@example.com", "key", 50, db_path=db_path)
    user = read_user("bob", db_path=db_path)
    assert user == ("bob", "pw", "bob@example.com", "key", 50)

    update_user_credits("bob", 75, db_path=db_path)
    assert read_user("bob", db_path=db_path)[4] == 75

    delete_user("bob", db_path=db_path)
    assert read_user("bob", db_path=db_path) is None

