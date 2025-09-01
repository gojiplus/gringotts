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

```python
from crud import create_user, read_user

# Create a user and look it up
create_user("alice", "password", "alice@example.com", "key123", 100)
print(read_user("alice"))
```

Run `python gringotts/example.py` for a more complete demonstration.

Automatically create when user is created
Add Credits



Call API Endpoint: 
Updates transactions table
Updates user table (total remaining credits)
	
How?
We log who made the call using the API Key
Checks if there are remaining credits. If yes, then calls the API and updates the tables. Otherwise 




### Authors
