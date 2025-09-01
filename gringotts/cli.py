import argparse

from . import auth, crud, db, models


def create_user(username: str, credits: int = 0):
    session = db.SessionLocal()
    try:
        user, api_key = auth.create_user_with_key(session, username, credits=credits)
        return user, api_key
    finally:
        session.close()


def add_credits(username: str, credits: int):
    session = db.SessionLocal()
    try:
        user = session.query(models.User).filter_by(username=username).first()
        if not user:
            raise ValueError("User not found")
        crud.update_user_credits(session, user, credits)
        return user
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description="Manage Gringotts users")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create-user", help="Create a new user")
    p_create.add_argument("username")
    p_create.add_argument("--credits", type=int, default=0)

    p_add = sub.add_parser("add-credits", help="Add credits to an existing user")
    p_add.add_argument("username")
    p_add.add_argument("credits", type=int)

    args = parser.parse_args()
    if args.cmd == "create-user":
        user, api_key = create_user(args.username, args.credits)
        print(f"Created user {user.username} with API key: {api_key}")
    elif args.cmd == "add-credits":
        user = add_credits(args.username, args.credits)
        print(f"User {user.username} now has {user.credits} credits")


if __name__ == "__main__":
    main()
