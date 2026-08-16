# API reference

The public surface of `gringotts`. Import everything from the top-level package:

```python
from gringotts import init_app, charge, GringottsConfig, CreditPack, grant
```

## Setup

```{eval-rst}
.. autofunction:: gringotts.init_app

.. autoclass:: gringotts.GringottsConfig
   :members:

.. autoclass:: gringotts.CreditPack
   :members:
```

## Charging

```{eval-rst}
.. autofunction:: gringotts.charge

.. autofunction:: gringotts.grant

.. autoclass:: gringotts.CreditedUser
   :members:
   :undoc-members:
```

## Errors

```{eval-rst}
.. autoclass:: gringotts.PaymentRequiredError
   :members:

.. autoclass:: gringotts.InvalidAPIKeyError
   :members:
```

## Wiring (advanced)

`init_app` handles wiring for you. These are exposed only for hosts that want to
share gringotts' database session:

```{eval-rst}
.. autofunction:: gringotts.get_session
```

- `gringotts.SessionLocal` — the SQLAlchemy ``sessionmaker`` bound to the
  configured database (`DATABASE_URL`).
- `gringotts.engine` — the SQLAlchemy ``Engine`` for that database (WAL and a
  busy timeout are applied automatically for SQLite).
- `gringotts.Base` — the declarative base gringotts' models attach to.

