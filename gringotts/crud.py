import sqlite3

def create_user(username, password, email, api_key, total_credits):
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO User (Username, Password, Email, APIKey, TotalRemainingCredits) VALUES (?, ?, ?, ?, ?)',
                   (username, password, email, api_key, total_credits))
    conn.commit()
    conn.close()

def read_user(username):
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM User WHERE Username = ?', (username,))
    user = cursor.fetchone()
    conn.close()
    return user

def update_user_credits(username, credits):
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE User SET TotalRemainingCredits = ? WHERE Username = ?', (credits, username))
    conn.commit()
    conn.close()

def delete_user(username):
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM User WHERE Username = ?', (username,))
    conn.commit()
    conn.close()
