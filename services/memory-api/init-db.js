const Database = require('better-sqlite3');
const fs = require('fs');
const path = require('path');

const DB_PATH = process.env.DB_PATH || path.join(__dirname, 'memory.db');
const SCHEMA_PATH = path.join(__dirname, 'schema.sql');

// Remove existing DB if any
if (fs.existsSync(DB_PATH)) {
    fs.unlinkSync(DB_PATH);
    console.log('Removed existing DB');
}

const db = new Database(DB_PATH);

// Execute schema as one block
const schema = fs.readFileSync(SCHEMA_PATH, 'utf8');

// PRAGMAs must go first, separately
db.pragma('journal_mode = WAL');
db.pragma('busy_timeout = 5000');
db.pragma('synchronous = NORMAL');

// Strip PRAGMA lines from schema and execute the rest
const ddl = schema
    .split('
')
    .filter(line => !line.trim().startsWith('PRAGMA'))
    .join('
');

db.exec(ddl);

// Verify
const tables = db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all();
console.log('Tables:', tables.map(t => t.name).join(', '));

const fts = db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'").all();
console.log('FTS:', fts.map(t => t.name).join(', '));

const triggers = db.prepare("SELECT name FROM sqlite_master WHERE type='trigger'").all();
console.log('Triggers:', triggers.map(t => t.name).join(', '));

console.log('WAL:', db.pragma('journal_mode', { simple: true }));

db.close();
console.log('
DB initialized at', DB_PATH);
