## Gringotts: Quick Monetization of Your API

Reduce time to monetize your API.

### Solution

We expand the API spec to add a cost field with price in terms of API credits and a translation for cost per credit. We also add an encrypted config to store your Stripe API Key.

### Outcome

Users can:

1. Create an API Key
2. Delete an API Key. Money is returned to the user account.
3. Add money to an API Key
4. Monitor their API credits remaining and the requests they made and how much each cost
5. Get response from an API if API Key has credits remaining. Otherwise error 'too few credits.'

### Quick Example

1. Start the API:

```bash
uvicorn gringotts.main:app
```

2. Create a user and API key:

```python
from gringotts import auth, db
db_session = db.SessionLocal()
user, api_key = auth.create_user_with_key(db_session, "alice", credits=5)
print(api_key)
```

3. Call the protected endpoint:

```bash
curl -X POST "http://localhost:8000/predict" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"input_string": "hello"}'
```

### Authors
