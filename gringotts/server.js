const express = require('express');
const sqlite3 = require('sqlite3').verbose();

const app = express();
const port = 3000;

// Connect to the SQLite database
const db = new sqlite3.Database('database.db');

// Define the routes

// Create a new user
app.post('/user', (req, res) => {
  const { username, password, email, apiKey, totalCredits } = req.body;
  const query = 'INSERT INTO User (Username, Password, Email, APIKey, TotalRemainingCredits) VALUES (?, ?, ?, ?, ?)';
  db.run(query, [username, password, email, apiKey, totalCredits], (err) => {
    if (err) {
      console.error(err);
      res.sendStatus(500);
    } else {
      res.sendStatus(201);
    }
  });
});

// Update user credits
app.patch('/user/:username/credits', (req, res) => {
  const { username } = req.params;
  const { credits } = req.body;
  const query = 'UPDATE User SET TotalRemainingCredits = ? WHERE Username = ?';
  db.run(query, [credits, username], (err) => {
    if (err) {
      console.error(err);
      res.sendStatus(500);
    } else {
      res.sendStatus(200);
    }
  });
});

// Delete a user
app.delete('/user/:username', (req, res) => {
  const { username } = req.params;
  const query = 'DELETE FROM User WHERE Username = ?';
  db.run(query, [username], (err) => {
    if (err) {
      console.error(err);
      res.sendStatus(500);
    } else {
      res.sendStatus(204);
    }
  });
});

// Start the server
app.listen(port, () => {
  console.log(`Server is listening on port ${port}`);
});
