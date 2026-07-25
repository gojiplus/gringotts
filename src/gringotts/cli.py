"""Command-line management: init-db, users, admin flags, and credits."""

import argparse

from . import auth, crud, db, models  # noqa: F401  (models import registers tables)


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and run the requested management command."""
    parser = argparse.ArgumentParser(
        prog="gringotts", description="Manage Gringotts users and credits"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Create the database tables")

    p_create = sub.add_parser(
        "create-user", help="Create a user and print the API key (once)"
    )
    p_create.add_argument("username")
    p_create.add_argument("--credits", type=int, default=0)
    p_create.add_argument(
        "--admin", action="store_true", help="Give the user admin access"
    )

    p_admin = sub.add_parser("set-admin", help="Grant or revoke admin access")
    p_admin.add_argument("username")
    p_admin.add_argument("--revoke", action="store_true")

    p_add = sub.add_parser("add-credits", help="Grant credits to a user")
    p_add.add_argument("username")
    p_add.add_argument("credits", type=int)

    p_balance = sub.add_parser("balance", help="Show a user's balance")
    p_balance.add_argument("username")

    args = parser.parse_args(argv)

    if args.cmd == "init-db":
        db.Base.metadata.create_all(bind=db.engine)
        print("Database tables created")
        return

    session = db.SessionLocal()
    try:
        if args.cmd == "create-user":
            user, api_key = auth.create_user_with_key(
                session, args.username, args.credits, is_admin=args.admin
            )
            role = " (admin)" if user.is_admin else ""
            print(f"Created user {user.username}{role} with {user.credits} credits")
            print(f"API key (shown once — save it now): {api_key}")
        elif args.cmd == "set-admin":
            user = crud.get_user_by_username(session, args.username)
            if user is None:
                raise SystemExit(f"User {args.username} not found")
            crud.set_admin(session, user, not args.revoke)
            state = "no longer" if args.revoke else "now"
            print(f"User {user.username} is {state} an admin")
        elif args.cmd == "add-credits":
            user = crud.get_user_by_username(session, args.username)
            if user is None:
                raise SystemExit(f"User {args.username} not found")
            crud.grant_credits(session, user, args.credits)
            print(f"User {user.username} now has {user.credits} credits")
        elif args.cmd == "balance":
            user = crud.get_user_by_username(session, args.username)
            if user is None:
                raise SystemExit(f"User {args.username} not found")
            last4 = user.key_last4
            print(f"User {user.username} has {user.credits} credits (key ...{last4})")
    finally:
        session.close()


if __name__ == "__main__":
    main()
