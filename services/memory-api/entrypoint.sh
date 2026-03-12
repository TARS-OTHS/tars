#!/bin/sh
# Check if DB has the memories table. If not, initialize it.
node -e "
const Database = require('better-sqlite3');
const db = new Database(process.env.DB_PATH || '/app/data/memory.db');
try {
    db.prepare('SELECT 1 FROM memories LIMIT 1').get();
    db.close();
    console.log('DB tables exist — skipping init');
} catch (e) {
    db.close();
    console.log('DB not initialized — running init-db.js');
    require('./init-db');
}
"

exec node server.js
