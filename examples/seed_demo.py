"""Seed a demo database with users and two weeks of simulated traffic.

Run:
    python examples/seed_demo.py
    uvicorn examples.demo_app:app

Then open http://localhost:8000/gringotts/admin with the printed admin key,
or http://localhost:8000/gringotts/account with any user key.
"""

import random
from datetime import datetime, timedelta, timezone

from gringotts import auth, crud, db, models

PACKS = [(100, 500), (1000, 4000)]
ENDPOINTS = ["/predict", "/classify", "/embed"]
USERS = ["ada", "grace", "alan", "edsger", "barbara"]


def main() -> None:
    db.Base.metadata.create_all(bind=db.engine)
    session = db.SessionLocal()
    rng = random.Random(42)
    try:
        if crud.get_user_by_username(session, "admin") is not None:
            raise SystemExit("Demo data already present — delete gringotts.db and rerun.")

        keys: dict[str, str] = {}
        _, admin_key = auth.create_user_with_key(session, "admin", credits=0, is_admin=True)
        keys["admin"] = admin_key

        event_counter = 0
        for name in USERS:
            user, key = auth.create_user_with_key(session, name, credits=rng.randint(20, 60))
            keys[name] = key
            for _ in range(rng.randint(15, 40)):
                cost = rng.choice([1, 1, 1, 2, 5])
                charged = crud.charge_user(session, user, cost, endpoint=rng.choice(ENDPOINTS))
                if charged and rng.random() < 0.05:
                    crud.refund_user(session, user, cost, endpoint=rng.choice(ENDPOINTS))
                if not charged or rng.random() < 0.06:
                    credits, cents = rng.choice(PACKS)
                    event_counter += 1
                    crud.grant_credits(
                        session,
                        user,
                        credits,
                        kind="purchase",
                        external_id=f"evt_demo_{event_counter}",
                        amount_cents=cents,
                    )

        # spread the generated activity over the past 14 days, oldest first
        transactions = (
            session.query(models.CreditTransaction).order_by(models.CreditTransaction.id).all()
        )
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=14)
        step = (now - start) / max(len(transactions), 1)
        for i, transaction in enumerate(transactions):
            transaction.created_at = start + step * i + timedelta(seconds=rng.randint(0, 300))
        session.commit()

        print(f"Seeded {len(USERS)} users and {len(transactions)} ledger entries.")
        print("API keys (shown once — the demo db stores only hashes):")
        for name, key in keys.items():
            label = "(admin)" if name == "admin" else ""
            print(f"  {name:<9}{label:<8} {key}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
