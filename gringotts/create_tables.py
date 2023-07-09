import sqlite3

# Connect to the database (creates a new database if it doesn't exist)
conn = sqlite3.connect('database.db')

# Create a cursor object to execute SQL statements
cursor = conn.cursor()

# Create the User table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS User (
        Username TEXT PRIMARY KEY,
        Password TEXT,
        Email TEXT,
        APIKey TEXT UNIQUE,
        TotalRemainingCredits INTEGER
    )
''')

# Create the API Calls table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS APICalls (
        APIKey TEXT,
        Date TEXT,
        Call TEXT,
        Cost INTEGER,
        FOREIGN KEY (APIKey) REFERENCES User (APIKey)
    )
''')

# Create the Transactions table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS Transactions (
        Username TEXT,
        Cost REAL,
        Date TEXT,
        FOREIGN KEY (Username) REFERENCES User (Username)
    )
''')

# Commit the changes and close the connection
conn.commit()
conn.close()

print("Tables created successfully!")
