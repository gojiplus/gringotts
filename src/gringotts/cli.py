"""Command-line management: init-db, users, admin flags, and credits."""

import argparse

from . import (  # noqa: F401  (models import registers tables)
    auth,
    crud,
    db,
    migrations,
    models,
)


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
    p_add.add_argument(
        "--idempotency-key",
        help="Apply this grant at most once, even if the command is retried",
    )

    p_balance = sub.add_parser("balance", help="Show a user's balance")
    p_balance.add_argument("username")

    sub.add_parser(
        "reconcile", help="Report any user whose balance disagrees with the ledger"
    )

    sub.add_parser(
        "migrate", help="Apply pending schema migrations to an existing database"
    )

    args = parser.parse_args(argv)

    if args.cmd == "init-db":
        db.Base.metadata.create_all(bind=db.engine)
        # Bring the schema to head. On a fresh DB this is a no-op that stamps the
        # version; on an existing DB it applies pending migrations rather than
        # falsely marking an out-of-date schema as current.
        migrations.run_pending(db.engine)
        print("Database tables created")
        return

    if args.cmd == "migrate":
        applied = migrations.run_pending(db.engine)
        if not applied:
            print("Database schema is up to date")
        else:
            for line in applied:
                print(f"Applied {line}")
        legacy = migrations.legacy_event_keyed_purchases(db.engine)
        if legacy:
            print(
                f"WARNING: {legacy} purchase(s) were recorded under the pre-0.2 "
                "event-id scheme. Drain any in-flight delayed (ACH) payments "
                "before relying on webhook idempotency — a settlement arriving "
                "after this upgrade could be credited twice."
            )
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
            if args.credits <= 0:
                raise SystemExit("credits to add must be positive")
            user = crud.get_user_by_username(session, args.username)
            if user is None:
                raise SystemExit(f"User {args.username} not found")
            applied = crud.grant_credits(
                session, user, args.credits, idempotency_key=args.idempotency_key
            )
            session.refresh(user)
            if not applied and args.idempotency_key:
                print(
                    f"Grant with key {args.idempotency_key!r} already applied; "
                    f"{user.username} has {user.credits} credits"
                )
            else:
                print(f"User {user.username} now has {user.credits} credits")
        elif args.cmd == "reconcile":
            discrepancies = crud.find_balance_discrepancies(session)
            if not discrepancies:
                print("All balances reconcile with the ledger")
            else:
                for d in discrepancies:
                    print(
                        f"MISMATCH user {d['username']} (id={d['id']}): "
                        f"balance={d['cached']} ledger={d['ledger']}"
                    )
                raise SystemExit(f"{len(discrepancies)} balance(s) do not reconcile")
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
