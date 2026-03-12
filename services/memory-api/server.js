const express = require('express');
const Database = require('better-sqlite3');
const { v4: uuidv4 } = require('uuid');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { callAnthropic, callAnthropicSync } = require('./anthropic-client');
// --- Embedding service client ---
const http = require('http');
const EMBEDDING_SERVICE = process.env.EMBEDDING_SERVICE_URL || 'http://127.0.0.1:8896';

function callEmbeddingService(endpoint, body) {
    return new Promise((resolve, reject) => {
        const data = JSON.stringify(body);
        const req = http.request(EMBEDDING_SERVICE + endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
            timeout: 30000,
        }, (res) => {
            let chunks = '';
            res.on('data', c => chunks += c);
            res.on('end', () => {
                if (res.statusCode !== 200) return reject(new Error('Embedding service ' + res.statusCode + ': ' + chunks));
                try { resolve(JSON.parse(chunks)); } catch(e) { reject(e); }
            });
        });
        req.on('error', reject);
        req.on('timeout', () => { req.destroy(); reject(new Error('Embedding service timeout')); });
        req.write(data);
        req.end();
    });
}

function callEmbeddingServiceSync(endpoint, body) {
    const { execFileSync } = require('child_process');
    const script = `
        const http = require('http');
        const data = JSON.stringify(JSON.parse(process.argv[1]));
        const req = http.request(process.argv[2] + process.argv[3], {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
            timeout: 30000,
        }, (res) => {
            let chunks = '';
            res.on('data', c => chunks += c);
            res.on('end', () => process.stdout.write(chunks));
        });
        req.on('error', e => { process.stderr.write(e.message); process.exit(1); });
        req.write(data);
        req.end();
    `;
    try {
        const result = execFileSync(process.execPath, ['-e', script, JSON.stringify(body), EMBEDDING_SERVICE, endpoint], {
            encoding: 'utf8', timeout: 30000, maxBuffer: 2 * 1024 * 1024,
        });
        return JSON.parse(result);
    } catch(e) {
        console.error('Embedding service sync error:', e.message);
        return null;
    }
}

async function getEmbedding(text) {
    // Async: get embedding vector for a single text, returns Buffer or null
    try {
        const result = await callEmbeddingService('/embed', { texts: [text] });
        if (result && result.embeddings && result.embeddings[0]) {
            return Buffer.from(new Float32Array(result.embeddings[0]).buffer);
        }
        return null;
    } catch (e) {
        console.error('Embedding service error:', e.message);
        return null;
    }
}

function cosineSimilarityVectors(a, b) {
    // a, b are Buffers containing Float32Array data
    const va = new Float32Array(a.buffer, a.byteOffset, a.byteLength / 4);
    const vb = new Float32Array(b.buffer, b.byteOffset, b.byteLength / 4);
    let dot = 0;
    for (let i = 0; i < va.length; i++) dot += va[i] * vb[i];
    return dot; // Normalized vectors, so dot product = cosine similarity
}

// --- Discord alerts ---
const OPS_ALERTS_CHANNEL = '1478653539004710954';
const DISCORD_TOKEN_PATH = process.env.DISCORD_TOKEN_PATH || '/app/secrets/discord-token';

function sendOpsAlert(message) {
    try {
        const token = fs.readFileSync(DISCORD_TOKEN_PATH, 'utf8').trim();
        const data = JSON.stringify({ content: message });
        const https = require('https');
        const req = https.request({
            hostname: 'discord.com',
            path: '/api/v10/channels/' + OPS_ALERTS_CHANNEL + '/messages',
            method: 'POST',
            headers: {
                'Authorization': 'Bot ' + token,
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(data),
            },
            timeout: 10000,
        }, (res) => {
            if (res.statusCode !== 200) {
                let body = '';
                res.on('data', c => body += c);
                res.on('end', () => console.error('Alert send failed:', res.statusCode, body));
            }
            res.resume();
        });
        req.on('error', (e) => console.error('Alert send error:', e.message));
        req.write(data);
        req.end();
    } catch (e) {
        console.error('sendOpsAlert error:', e.message);
    }
}


// --- Config ---
const PORT = parseInt(process.env.PORT || '8897');
const BIND_HOST = process.env.BIND_HOST || '0.0.0.0';
const DB_PATH = process.env.DB_PATH || path.join(__dirname, 'memory.db');
const AGENTS_PATH = path.join(__dirname, 'agents.json');
const MAX_CC_CONCURRENT = parseInt(process.env.MAX_CC_CONCURRENT || '5');

// --- Database ---
const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');
db.pragma('busy_timeout = 5000');
db.pragma('synchronous = NORMAL');

// --- Agent Registry ---
const agentsConfig = JSON.parse(fs.readFileSync(AGENTS_PATH, 'utf8'));

function getAgentScopes(agentId) {
    const agent = agentsConfig.agents[agentId];
    const scopes = [];
    if (agent) {
        // Only include global scope if agent has globalAccess (default: true)
        if (agent.globalAccess !== false) {
            scopes.push('global');
        }
        scopes.push(`agent:${agentId}`);
        for (const group of agent.groups || []) {
            scopes.push(`group:${group}`);
        }
    } else {
        scopes.push('global');
    }
    return scopes;
}


// --- Write Queue ---
class WriteQueue {
    constructor() {
        this.queue = [];
        this.processing = false;
    }

    enqueue(fn) {
        return new Promise((resolve, reject) => {
            this.queue.push({ fn, resolve, reject });
            this._process();
        });
    }

    async _process() {
        if (this.processing) return;
        this.processing = true;
        while (this.queue.length > 0) {
            const { fn, resolve, reject } = this.queue.shift();
            try {
                resolve(await fn());
            } catch (err) {
                reject(err);
            }
        }
        this.processing = false;
    }

    get depth() {
        return this.queue.length;
    }
}

const writeQueue = new WriteQueue();

// --- Changelog helper ---
function logChange(agent, tableName, action, recordId, oldValue, newValue, reason) {
    db.prepare(`
        INSERT INTO changelog (agent, table_name, action, record_id, old_value, new_value, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    `).run(agent, tableName, action, recordId,
        oldValue ? JSON.stringify(oldValue) : null,
        newValue ? JSON.stringify(newValue) : null,
        reason || null
    );
}

// --- Prepared statements ---
const stmts = {
    insertMemory: db.prepare(`
        INSERT INTO memories (id, type, content, category, confidence, created_by, scope, scope_target, tags, metadata, pinned, last_accessed, embedding)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
    `),
    updateMemory: db.prepare(`
        UPDATE memories SET content=?, category=?, confidence=?, scope=?, scope_target=?, tags=?, metadata=?, pinned=?, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    `),
    deleteMemory: db.prepare(`DELETE FROM memories WHERE id=?`),
    getMemory: db.prepare(`SELECT * FROM memories WHERE id=?`),

    insertRelationship: db.prepare(`
        INSERT INTO relationships (id, entity_a, relationship, entity_b, confidence, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
    `),
    deleteRelationship: db.prepare(`DELETE FROM relationships WHERE id=?`),
    getRelationship: db.prepare(`SELECT * FROM relationships WHERE id=?`),

    insertTask: db.prepare(`
        INSERT INTO tasks (id, title, description, status, priority, assigned_to, created_by, context, tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    `),
    updateTask: db.prepare(`
        UPDATE tasks SET title=COALESCE(?,title), description=COALESCE(?,description),
        status=COALESCE(?,status), priority=COALESCE(?,priority), assigned_to=COALESCE(?,assigned_to),
        started_at=CASE WHEN ?='in_progress' AND started_at IS NULL THEN CURRENT_TIMESTAMP ELSE started_at END,
        completed_at=CASE WHEN ? IN ('completed','failed') THEN CURRENT_TIMESTAMP ELSE completed_at END,
        last_heartbeat=CURRENT_TIMESTAMP
        WHERE id=?
    `),
    deleteTask: db.prepare(`DELETE FROM tasks WHERE id=?`),
    getTask: db.prepare(`SELECT * FROM tasks WHERE id=?`),

    insertTracking: db.prepare(`
        INSERT INTO tracking (id, type, date, time, data, notes, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    `),
    deleteTracking: db.prepare(`DELETE FROM tracking WHERE id=?`),
    getTracking: db.prepare(`SELECT * FROM tracking WHERE id=?`),

    insertPerson: db.prepare(`
        INSERT INTO people (id, name, relationship, context, notes, contact_info, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    `),
    updatePerson: db.prepare(`
        UPDATE people SET name=COALESCE(?,name), relationship=COALESCE(?,relationship),
        context=COALESCE(?,context), notes=COALESCE(?,notes),
        contact_info=COALESCE(?,contact_info), metadata=COALESCE(?,metadata),
        updated_at=CURRENT_TIMESTAMP WHERE id=?
    `),
    deletePerson: db.prepare(`DELETE FROM people WHERE id=?`),
    getPerson: db.prepare(`SELECT * FROM people WHERE id=?`),

    insertProject: db.prepare(`
        INSERT INTO projects (id, name, status, description, links, notes, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    `),
    updateProject: db.prepare(`
        UPDATE projects SET name=COALESCE(?,name), status=COALESCE(?,status),
        description=COALESCE(?,description), links=COALESCE(?,links),
        notes=COALESCE(?,notes), metadata=COALESCE(?,metadata),
        updated_at=CURRENT_TIMESTAMP WHERE id=?
    `),
    deleteProject: db.prepare(`DELETE FROM projects WHERE id=?`),
    getProject: db.prepare(`SELECT * FROM projects WHERE id=?`),

    insertTaskReview: db.prepare(`
        INSERT INTO task_reviews (id, task_id, agent_id, success, time_taken_seconds, model_used,
        estimated_complexity, actual_complexity, what_failed, what_to_repeat, routing_suggestion)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `),

    searchFts: db.prepare(`
        SELECT m.*, rank
        FROM memories_fts f
        JOIN memories m ON m.rowid = f.rowid
        WHERE memories_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    `),

    bumpAccess: db.prepare(`
        UPDATE memories SET access_count = access_count + 1, last_accessed = CURRENT_TIMESTAMP WHERE id = ?
    `),

    memoryStats: db.prepare(`
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN type='semantic' THEN 1 ELSE 0 END) as semantic,
            SUM(CASE WHEN type='episodic' THEN 1 ELSE 0 END) as episodic,
            SUM(CASE WHEN type='procedural' THEN 1 ELSE 0 END) as procedural,
            AVG(confidence) as avg_confidence,
            SUM(CASE WHEN confidence >= 0.8 THEN 1 ELSE 0 END) as high_confidence,
            SUM(CASE WHEN confidence < 0.3 AND pinned = 0 THEN 1 ELSE 0 END) as decaying,
            SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) as pinned
        FROM memories WHERE scope != 'archived'
    `),

    archivedCount: db.prepare(`SELECT COUNT(*) as count FROM memories WHERE scope = 'archived'`),

    conflictsCount: db.prepare(`SELECT COUNT(*) as count FROM memories WHERE json_extract(metadata, '$.conflict') = 1`),

    recentChangelog: db.prepare(`
        SELECT action, COUNT(*) as count
        FROM changelog
        WHERE timestamp > datetime('now', '-24 hours')
        AND table_name = 'memories'
        GROUP BY action
    `),
};

// --- Similarity / Dedup helpers ---
function tokenize(text) {
    return text.toLowerCase().replace(/[^a-z0-9\s]/g, '').split(/\s+/).filter(Boolean);
}

function computeSimilarity(a, b) {
    // Jaccard fallback
    const tokensA = new Set(tokenize(a));
    const tokensB = new Set(tokenize(b));
    if (tokensA.size === 0 || tokensB.size === 0) return 0;
    let intersection = 0;
    for (const t of tokensA) {
        if (tokensB.has(t)) intersection++;
    }
    return intersection / Math.max(tokensA.size, tokensB.size);
}

function computeSimilarityWithEmbeddings(textA, textB, embA, embB) {
    // If both embeddings available, use cosine similarity
    if (embA && embB) {
        return cosineSimilarityVectors(embA, embB);
    }
    // Fallback to Jaccard
    return computeSimilarity(textA, textB);
}

function findSimilarMemories(content, category = null, limit = 10) {
    try {
        // Quote each word to prevent FTS5 operator interpretation (OR, AND, NOT, NEAR)
        const words = content.replace(/[^a-zA-Z0-9\s]/g, ' ').trim().split(/\s+/).filter(Boolean);
        if (words.length === 0) return [];
        // Use top keywords (sorted by length desc to prioritize meaningful words)
        const topWords = words.sort((a, b) => b.length - a.length).slice(0, 15);
        const cleaned = topWords.map(w => `"${w}"`).join(' OR ');
        if (!cleaned) return [];
        if (category) {
            return db.prepare(`
                SELECT m.id, m.content, m.confidence, m.access_count, m.type, m.category
                FROM memories_fts f
                JOIN memories m ON m.rowid = f.rowid
                WHERE memories_fts MATCH ?
                AND m.scope != 'archived'
                AND m.category = ?
                ORDER BY rank
                LIMIT ?
            `).all(cleaned, category, limit);
        }
        return db.prepare(`
            SELECT m.id, m.content, m.confidence, m.access_count, m.type, m.category
            FROM memories_fts f
            JOIN memories m ON m.rowid = f.rowid
            WHERE memories_fts MATCH ?
            AND m.scope != 'archived'
            ORDER BY rank
            LIMIT ?
        `).all(cleaned, limit);
    } catch (e) {
        return [];
    }
}

async function dedupInsert(data, agent, reason) {
    // Get embedding for the new content
    const newEmbedding = await getEmbedding(data.content);

    // Search within same category first, then globally
    const categoryMatches = data.category ? findSimilarMemories(data.content, data.category, 10) : [];
    const globalMatches = findSimilarMemories(data.content, null, 10);

    // Merge and deduplicate candidates
    const seen = new Set();
    const similar = [];
    for (const m of [...categoryMatches, ...globalMatches]) {
        if (!seen.has(m.id)) {
            seen.add(m.id);
            similar.push(m);
        }
    }

    for (const existing of similar) {
        const sim = computeSimilarityWithEmbeddings(existing.content, data.content, existing.embedding, newEmbedding);
        if (sim > 0.85) {
            // Reinforce — boost confidence, bump access, link
            const newConf = Math.min(1.0, existing.confidence + 0.1);
            db.prepare(`
                UPDATE memories
                SET confidence = ?, access_count = access_count + 1,
                    last_accessed = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            `).run(newConf, existing.id);

            logChange(agent, 'memories', 'update', existing.id,
                { confidence: existing.confidence },
                { confidence: newConf },
                `reinforced by dedup (similarity ${sim.toFixed(2)})`
            );

            return { action: 'reinforced', id: existing.id, similarity: sim, new_confidence: newConf };
        }
    }

    // No match — insert as new with embedding
    const id = data.id || uuidv4();
    stmts.insertMemory.run(
        id, data.type, data.content, data.category || null,
        data.confidence ?? 0.5, agent, data.scope || 'group:ops',
        data.scope_target || null, data.tags ? JSON.stringify(data.tags) : null,
        data.metadata ? JSON.stringify(data.metadata) : null, data.pinned ? 1 : 0,
        newEmbedding
    );
    logChange(agent, 'memories', 'insert', id, null, data, reason);
    return { action: 'created', id };
}

// --- Express app ---
const app = express();
app.use(express.json({ limit: '1mb' }));

// Request logging
app.use((req, res, next) => {
    const start = Date.now();
    res.on('finish', () => {
        console.log(`${req.method} ${req.path} ${res.statusCode} ${Date.now() - start}ms`);
    });
    next();
});

// --- POST /memory/write ---
app.post('/memory/write', async (req, res) => {
    try {
        const { table, action, data, agent, reason } = req.body;
        if (!table || !action || !data || !agent) {
            return res.status(400).json({ error: 'Missing required fields: table, action, data, agent' });
        }

        // Validate ID for update/delete actions
        if ((action === 'update' || action === 'delete') && !data.id) {
            return res.status(400).json({ error: `Missing data.id — required for ${action} on ${table}` });
        }

        const result = await writeQueue.enqueue(async () => {
            const id = data.id || uuidv4();

            switch (table) {
                case 'memories': {
                    if (action === 'insert') {
                        // Dedup-aware insert: reinforce if similar exists, else create new
                        const dedupResult = await dedupInsert(data, agent, reason);
                        return dedupResult;
                    } else if (action === 'update') {
                        const old = stmts.getMemory.get(data.id);
                        if (!old) throw new Error(`Memory ${data.id} not found`);
                        stmts.updateMemory.run(
                            data.content ?? old.content, data.category ?? old.category,
                            data.confidence ?? old.confidence, data.scope ?? old.scope,
                            data.scope_target ?? old.scope_target, data.tags ? JSON.stringify(data.tags) : old.tags,
                            data.metadata ? JSON.stringify(data.metadata) : old.metadata,
                            data.pinned !== undefined ? (data.pinned ? 1 : 0) : old.pinned, data.id
                        );
                        logChange(agent, 'memories', 'update', data.id, old, data, reason);
                        return { id: data.id };
                    } else if (action === 'delete') {
                        const old = stmts.getMemory.get(data.id);
                        stmts.deleteMemory.run(data.id);
                        logChange(agent, 'memories', 'delete', data.id, old, null, reason);
                        return { id: data.id };
                    }
                    break;
                }

                case 'relationships': {
                    if (action === 'insert') {
                        stmts.insertRelationship.run(id, data.entity_a, data.relationship, data.entity_b, data.confidence ?? 0.5, agent);
                        logChange(agent, 'relationships', 'insert', id, null, data, reason);
                        return { id };
                    } else if (action === 'delete') {
                        const old = stmts.getRelationship.get(data.id);
                        stmts.deleteRelationship.run(data.id);
                        logChange(agent, 'relationships', 'delete', data.id, old, null, reason);
                        return { id: data.id };
                    }
                    break;
                }

                case 'tasks': {
                    if (action === 'insert') {
                        stmts.insertTask.run(
                            id, data.title, data.description || null, data.status || 'pending',
                            data.priority ?? 3, data.assigned_to || null, agent,
                            data.context ? JSON.stringify(data.context) : null,
                            data.tags ? JSON.stringify(data.tags) : null
                        );
                        logChange(agent, 'tasks', 'insert', id, null, data, reason);
                        return { id };
                    } else if (action === 'update') {
                        const old = stmts.getTask.get(data.id);
                        if (!old) throw new Error(`Task ${data.id} not found`);
                        const newStatus = data.status || null;
                        stmts.updateTask.run(
                            data.title || null, data.description || null,
                            newStatus, data.priority || null, data.assigned_to || null,
                            newStatus, newStatus, data.id
                        );
                        logChange(agent, 'tasks', 'update', data.id, old, data, reason);
                        return { id: data.id };
                    } else if (action === 'delete') {
                        const old = stmts.getTask.get(data.id);
                        stmts.deleteTask.run(data.id);
                        logChange(agent, 'tasks', 'delete', data.id, old, null, reason);
                        return { id: data.id };
                    }
                    break;
                }

                case 'tracking': {
                    if (action === 'insert') {
                        stmts.insertTracking.run(
                            id, data.type, data.date, data.time || null,
                            JSON.stringify(data.data), data.notes || null, agent
                        );
                        logChange(agent, 'tracking', 'insert', id, null, data, reason);
                        return { id };
                    } else if (action === 'delete') {
                        const old = stmts.getTracking.get(data.id);
                        stmts.deleteTracking.run(data.id);
                        logChange(agent, 'tracking', 'delete', data.id, old, null, reason);
                        return { id: data.id };
                    }
                    break;
                }

                case 'people': {
                    if (action === 'insert') {
                        stmts.insertPerson.run(
                            id, data.name, data.relationship || null, data.context || null,
                            data.notes || null, data.contact_info ? JSON.stringify(data.contact_info) : null,
                            data.metadata ? JSON.stringify(data.metadata) : null
                        );
                        logChange(agent, 'people', 'insert', id, null, data, reason);
                        return { id };
                    } else if (action === 'update') {
                        const old = stmts.getPerson.get(data.id);
                        if (!old) throw new Error(`Person ${data.id} not found`);
                        stmts.updatePerson.run(
                            data.name || null, data.relationship || null, data.context || null,
                            data.notes || null, data.contact_info ? JSON.stringify(data.contact_info) : null,
                            data.metadata ? JSON.stringify(data.metadata) : null, data.id
                        );
                        logChange(agent, 'people', 'update', data.id, old, data, reason);
                        return { id: data.id };
                    } else if (action === 'delete') {
                        const old = stmts.getPerson.get(data.id);
                        stmts.deletePerson.run(data.id);
                        logChange(agent, 'people', 'delete', data.id, old, null, reason);
                        return { id: data.id };
                    }
                    break;
                }

                case 'projects': {
                    if (action === 'insert') {
                        stmts.insertProject.run(
                            id, data.name, data.status || 'active', data.description || null,
                            data.links ? JSON.stringify(data.links) : null, data.notes || null,
                            data.metadata ? JSON.stringify(data.metadata) : null
                        );
                        logChange(agent, 'projects', 'insert', id, null, data, reason);
                        return { id };
                    } else if (action === 'update') {
                        const old = stmts.getProject.get(data.id);
                        if (!old) throw new Error(`Project ${data.id} not found`);
                        stmts.updateProject.run(
                            data.name || null, data.status || null, data.description || null,
                            data.links ? JSON.stringify(data.links) : null, data.notes || null,
                            data.metadata ? JSON.stringify(data.metadata) : null, data.id
                        );
                        logChange(agent, 'projects', 'update', data.id, old, data, reason);
                        return { id: data.id };
                    } else if (action === 'delete') {
                        const old = stmts.getProject.get(data.id);
                        stmts.deleteProject.run(data.id);
                        logChange(agent, 'projects', 'delete', data.id, old, null, reason);
                        return { id: data.id };
                    }
                    break;
                }

                case 'task_reviews': {
                    if (action === 'insert') {
                        stmts.insertTaskReview.run(
                            id, data.task_id, data.agent_id || agent, data.success ? 1 : 0,
                            data.time_taken_seconds || null, data.model_used || null,
                            data.estimated_complexity || null, data.actual_complexity || null,
                            data.what_failed || null, data.what_to_repeat || null,
                            data.routing_suggestion || null
                        );
                        logChange(agent, 'task_reviews', 'insert', id, null, data, reason);
                        return { id };
                    }
                    break;
                }

                default:
                    throw new Error(`Unknown table: ${table}`);
            }
            throw new Error(`Unsupported action '${action}' for table '${table}'`);
        });

        res.json({ success: true, ...result });
    } catch (err) {
        console.error('Write error:', err.message);
        res.status(400).json({ error: err.message });
    }
});

// --- GET /memory/query ---
app.get('/memory/query', (req, res) => {
    try {
        const { sql, params, agent } = req.query;
        if (!sql) return res.status(400).json({ error: 'Missing sql parameter' });

        // Only allow SELECT
        const trimmed = sql.trim().toUpperCase();
        if (!trimmed.startsWith('SELECT') && !trimmed.startsWith('WITH')) {
            return res.status(403).json({ error: 'Only SELECT statements allowed' });
        }
        // Block dangerous keywords
        const blocked = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE', 'ATTACH', 'DETACH', 'PRAGMA'];
        for (const kw of blocked) {
            if (trimmed.includes(kw)) {
                return res.status(403).json({ error: `Blocked keyword: ${kw}` });
            }
        }

        const bindParams = params ? JSON.parse(params) : [];
        const rows = db.prepare(sql).all(...bindParams);
        res.json({ rows });
    } catch (err) {
        console.error('Query error:', err.message);
        res.status(400).json({ error: err.message });
    }
});

// --- GET /memory/search ---
app.get('/memory/search', (req, res) => {
    try {
        const { q, type, scope, agent, limit: limitStr, after, before, sort } = req.query;
        if (!q) return res.status(400).json({ error: 'Missing q parameter' });

        const limit = parseInt(limitStr) || 10;

        // Temporal filters
        let timeFilter = '';
        let timeParams = [];
        const resolveRelativeTime = (val) => {
            const match = val.match(/^(\d+)([dhm])$/);
            if (match) {
                const n = parseInt(match[1]);
                const unit = { d: 'days', h: 'hours', m: 'minutes' }[match[2]];
                return new Date(Date.now() - n * { days: 86400000, hours: 3600000, minutes: 60000 }[unit]).toISOString();
            }
            return val; // Assume ISO date
        };
        if (after) {
            timeFilter += ' AND m.created_at >= ?';
            timeParams.push(resolveRelativeTime(after));
        }
        if (before) {
            timeFilter += ' AND m.created_at <= ?';
            timeParams.push(resolveRelativeTime(before));
        }

        // Build scope filter from agent registry
        let scopeFilter = '';
        let scopeParams = [];
        if (agent && agentsConfig.agents[agent]) {
            const scopes = getAgentScopes(agent);
            const placeholders = scopes.map(() => '?').join(',');
            scopeFilter = `AND (m.scope IN (${placeholders}) OR (m.scope = 'agent' AND m.scope_target = ?))`;
            scopeParams = [...scopes, agent];
        }

        let typeFilter = '';
        if (type) {
            typeFilter = 'AND m.type = ?';
        }

        // FTS search with temporal filters
        const orderBy = sort === 'time' ? 'ORDER BY m.created_at DESC' : 'ORDER BY rank';
        const searchSql = `
            SELECT m.*, rank
            FROM memories_fts f
            JOIN memories m ON m.rowid = f.rowid
            WHERE memories_fts MATCH ?
            AND m.scope != 'archived'
            ${scopeFilter}
            ${typeFilter}
            ${timeFilter}
            ${orderBy}
            LIMIT ?
        `;

        const allParams = [q, ...scopeParams, ...(type ? [type] : []), ...timeParams, limit];
        const results = db.prepare(searchSql).all(...allParams);

        // Bump access counts
        for (const row of results) {
            stmts.bumpAccess.run(row.id);
        }

        res.json({ results });
    } catch (err) {
        console.error('Search error:', err.message);
        res.status(400).json({ error: err.message });
    }
});

// --- GET /memory/context ---
app.get('/memory/context', (req, res) => {
    try {
        const { agent, limit: limitStr } = req.query;
        if (!agent) return res.status(400).json({ error: 'Missing agent parameter' });

        const limit = parseInt(limitStr) || 20;
        const scopes = getAgentScopes(agent);
        const placeholders = scopes.map(() => '?').join(',');

        // Pinned memories (global + agent's scopes)
        const pinned = db.prepare(`
            SELECT * FROM memories
            WHERE pinned = 1 AND scope IN (${placeholders})
        `).all(...scopes);

        // Top recent high-confidence memories
        const recent = db.prepare(`
            SELECT * FROM memories
            WHERE pinned = 0 AND scope != 'archived'
            AND scope IN (${placeholders})
            AND confidence >= 0.3
            AND last_accessed > datetime('now', '-7 days')
            ORDER BY confidence DESC, last_accessed DESC
            LIMIT ?
        `).all(...scopes, limit);

        // Unresolved conflicts (stored as metadata flag)
        const conflicts = db.prepare(`
            SELECT * FROM memories
            WHERE json_extract(metadata, '$.conflict') = 1
            AND scope IN (${placeholders})
        `).all(...scopes);

        // Active tasks for this agent
        const tasks = db.prepare(`
            SELECT * FROM tasks
            WHERE assigned_to = ? AND status IN ('pending', 'assigned', 'in_progress')
            ORDER BY priority ASC, created_at ASC
        `).all(agent);

        // Bump access on returned memories
        for (const m of [...pinned, ...recent]) {
            stmts.bumpAccess.run(m.id);
        }

        res.json({ pinned, recent, conflicts, tasks });
    } catch (err) {
        console.error('Context error:', err.message);
        res.status(400).json({ error: err.message });
    }
});

// --- GET /agents ---
app.get('/agents', (req, res) => {
    res.json(agentsConfig);
});

// --- GET /status ---
app.get('/status', (req, res) => {
    try {
        const stats = stmts.memoryStats.get();
        const archived = stmts.archivedCount.get();
        const conflicts = stmts.conflictsCount.get();
        const recentActions = stmts.recentChangelog.all();
        const uptime = process.uptime();

        // Parse recent changelog into counts
        const last24h = { inserts: 0, updates: 0, deletes: 0 };
        for (const row of recentActions) {
            if (row.action === 'insert') last24h.inserts = row.count;
            else if (row.action === 'update') last24h.updates = row.count;
            else if (row.action === 'delete') last24h.deletes = row.count;
        }

        // DB file size
        let dbSizeMb = 0;
        try {
            const stat = fs.statSync(DB_PATH);
            dbSizeMb = parseFloat((stat.size / (1024 * 1024)).toFixed(2));
        } catch (e) {}

        res.json({
            uptime: Math.floor(uptime),
            write_queue_depth: writeQueue.depth,
            memory_stats: {
                total: stats.total || 0,
                by_type: {
                    semantic: stats.semantic || 0,
                    episodic: stats.episodic || 0,
                    procedural: stats.procedural || 0,
                },
                avg_confidence: stats.avg_confidence ? parseFloat(stats.avg_confidence.toFixed(3)) : 0,
                high_confidence: stats.high_confidence || 0,
                decaying: stats.decaying || 0,
                pinned: stats.pinned || 0,
                archived: archived.count || 0,
                conflicts_unresolved: conflicts.count || 0,
                last_24h: last24h,
                db_size_mb: dbSizeMb,
            },
            agents: Object.keys(agentsConfig.agents),
            session_states: (() => {
                try {
                    const rows = db.prepare(`
                        SELECT scope, content, updated_at FROM memories
                        WHERE type = 'episodic' AND category = 'session' AND scope LIKE 'agent:%'
                        ORDER BY updated_at DESC
                    `).all();
                    return rows.map(r => {
                        // scope format: agent:main or agent:main:channel:discord:channel:123
                        const parts = r.scope.split(':');
                        const agent = parts[1] || 'unknown';
                        let channel = 'global';
                        if (parts.length >= 4 && parts[2] === 'channel') {
                            channel = parts.slice(3).join(':');
                        } else if (parts.length >= 3) {
                            channel = parts.slice(2).join(':');
                        }
                        let parsed;
                        try { parsed = JSON.parse(r.content); } catch { parsed = null; }
                        return {
                            agent,
                            channel,
                            task: parsed?.task_summary || null,
                            status: parsed?.status || null,
                            updated_at: r.updated_at,
                        };
                    });
                } catch(e) { return []; }
            })(),
            services: {
                agent_services_uptime: Math.floor(process.uptime()),
                pid: process.pid,
                memory_usage_mb: Math.round(process.memoryUsage().rss / (1024 * 1024)),
            },
            broker: {
                active_cc_tasks: getActiveTaskCount(),
                max_concurrent: MAX_CC_CONCURRENT,
            },
        });
    } catch (err) {
        console.error('Status error:', err.message);
        res.status(500).json({ error: err.message });
    }
});

// --- POST /memory/extract ---
app.post('/memory/extract', async (req, res) => {
    try {
        const { conversation, agent, session_id } = req.body;
        if (!conversation || !agent) {
            return res.status(400).json({ error: 'Missing conversation or agent' });
        }

        // Build known-facts context to prevent re-extraction of established knowledge
        let knownFactsSection = '';
        try {
            const knownFacts = db.prepare(`
                SELECT content FROM memories 
                WHERE confidence >= 0.7 AND type IN ('semantic', 'procedural') AND scope != 'archived'
                ORDER BY access_count DESC LIMIT 50
            `).all();
            if (knownFacts.length > 0) {
                const factsList = knownFacts.map(f => '- ' + f.content.substring(0, 120)).join('\n');
                knownFactsSection = `\n\nALREADY KNOWN (DO NOT re-extract these or similar facts):\n${factsList}\n`;
            }
        } catch (e) {
            console.error('Failed to load known facts for extraction:', e.message);
        }

        const extractionPrompt = `Extract discrete facts AND entity relationships from this conversation.

FACTS: For each fact, classify as:
- semantic: factual knowledge, preferences, attributes
- episodic: what happened, decisions made, events
- procedural: how to do something, rules, workflows

Category for each fact:
- user: facts about the user (Peter)
- people: facts about other people
- system: infrastructure, tools, procedures
- project: project-specific knowledge
- business: business/work related
- episodic: one-time events

RELATIONSHIPS: Also extract entity relationships — who/what is connected to who/what and how.
Common relationship types: owns, manages, uses, lives_in, works_on, created_by, part_of, related_to, son_of, friend_of, located_at, depends_on, configured_with

Return ONLY a JSON object with two arrays:
{
  "facts": [{"type":"semantic|episodic|procedural","content":"the fact","category":"user|people|system|project|business|episodic"}],
  "relationships": [{"entity_a":"Thing1","relationship":"verb_phrase","entity_b":"Thing2"}]
}

${knownFactsSection}
Rules:
- Facts: Be specific and atomic — one fact per entry. Ignore small talk and filler. Focus on things worth remembering long-term. Do NOT extract facts that are already known (listed above).
- Relationships: Use lowercase_snake_case for relationship names. Normalize entity names (e.g. "Peter" not "peter" or "Peter Trueman" unless full name is relevant). Only extract relationships clearly stated or strongly implied.

Conversation:
${conversation}`;

        // Call Anthropic API for extraction
        let rawOutput;
        try {
            rawOutput = await callAnthropic(extractionPrompt, { timeoutMs: 60000 });
        } catch (err) {
            sendOpsAlert(`⚠️ **Memory extract failed:** ${err.message}`);
            return res.status(500).json({ error: `Anthropic API failed: ${err.message}` });
        }

        // Parse JSON from output — handle markdown code blocks
        let parsed;
        try {
            const jsonMatch = rawOutput.match(/\{[\s\S]*\}/);
            if (!jsonMatch) throw new Error('No JSON object found in output');
            parsed = JSON.parse(jsonMatch[0]);
        } catch (err) {
            // Fallback: try parsing as array (old format compatibility)
            try {
                const arrMatch = rawOutput.match(/\[[\s\S]*\]/);
                if (arrMatch) {
                    parsed = { facts: JSON.parse(arrMatch[0]), relationships: [] };
                } else {
                    throw err;
                }
            } catch (err2) {
                return res.status(500).json({ error: `Failed to parse extraction: ${err.message}`, raw: rawOutput.substring(0, 500) });
            }
        }

        const facts = parsed.facts || [];
        const relationships = parsed.relationships || [];

        // Process each extracted fact through dedup
        const results = { extracted: [], merged: 0, new: 0, conflicts: [], relationships_added: 0, relationships_reinforced: 0 };

        await writeQueue.enqueue(async () => {
            // Process facts
            for (const fact of facts) {
                if (!fact.content || !fact.type) continue;

                const dedupResult = await dedupInsert({
                    type: fact.type,
                    content: fact.content,
                    category: fact.category || null,
                    scope: 'global',
                    metadata: session_id ? { session_id } : null,
                }, agent, 'auto-extracted from conversation');

                results.extracted.push({ ...fact, ...dedupResult });
                if (dedupResult.action === 'reinforced') results.merged++;
                else results.new++;
            }

            // Process relationships
            for (const rel of relationships) {
                if (!rel.entity_a || !rel.relationship || !rel.entity_b) continue;

                const ea = rel.entity_a.trim();
                const r = rel.relationship.trim().toLowerCase();
                const eb = rel.entity_b.trim();

                // Check if relationship already exists
                const existing = db.prepare(
                    'SELECT id, confidence FROM relationships WHERE entity_a = ? AND relationship = ? AND entity_b = ?'
                ).get(ea, r, eb);

                if (existing) {
                    // Reinforce confidence
                    const newConf = Math.min(1.0, existing.confidence + 0.1);
                    db.prepare('UPDATE relationships SET confidence = ? WHERE id = ?').run(newConf, existing.id);
                    results.relationships_reinforced++;
                } else {
                    // Insert new relationship
                    const relId = 'rel_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6);
                    db.prepare(
                        'INSERT INTO relationships (id, entity_a, relationship, entity_b, confidence, created_by) VALUES (?, ?, ?, ?, 0.5, ?)'
                    ).run(relId, ea, r, eb, agent);
                    results.relationships_added++;
                }
            }
        });

        res.json(results);
    } catch (err) {
        console.error('Extract error:', err.message);
        res.status(500).json({ error: err.message });
    }
});


// --- GET /memory/relationships ---
app.get('/memory/relationships', (req, res) => {
    try {
        const { entity, relationship, depth } = req.query;
        const maxDepth = Math.min(parseInt(depth) || 1, 3);

        if (!entity) {
            // Return all relationships
            const all = db.prepare('SELECT * FROM relationships ORDER BY confidence DESC LIMIT 100').all();
            return res.json({ relationships: all, count: all.length });
        }

        if (maxDepth === 1) {
            // Direct relationships only
            let query = `SELECT * FROM relationships WHERE entity_a = ? OR entity_b = ?`;
            const params = [entity, entity];
            if (relationship) {
                query += ' AND relationship = ?';
                params.push(relationship);
            }
            query += ' ORDER BY confidence DESC';
            const rels = db.prepare(query).all(...params);
            return res.json({ entity, relationships: rels, count: rels.length });
        }

        // Multi-hop traversal with dedup
        const visited = new Set();
        const seenRels = new Set();
        const allRels = [];
        let frontier = [entity];

        for (let d = 0; d < maxDepth; d++) {
            const nextFrontier = [];
            for (const e of frontier) {
                if (visited.has(e)) continue;
                visited.add(e);
                const rels = db.prepare(
                    'SELECT * FROM relationships WHERE entity_a = ? OR entity_b = ? ORDER BY confidence DESC'
                ).all(e, e);
                for (const r of rels) {
                    if (!seenRels.has(r.id)) {
                        seenRels.add(r.id);
                        allRels.push({ ...r, depth: d + 1 });
                    }
                    const other = r.entity_a === e ? r.entity_b : r.entity_a;
                    if (!visited.has(other)) nextFrontier.push(other);
                }
            }
            frontier = nextFrontier;
        }

        res.json({ entity, depth: maxDepth, relationships: allRels, entities_visited: [...visited], count: allRels.length });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// --- GET /memory/entities ---
app.get('/memory/entities', (req, res) => {
    try {
        const entities = db.prepare(`
            SELECT entity, COUNT(*) as connection_count FROM (
                SELECT entity_a as entity FROM relationships
                UNION ALL
                SELECT entity_b as entity FROM relationships
            ) GROUP BY entity ORDER BY connection_count DESC
        `).all();
        res.json({ entities, count: entities.length });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// --- GET /memory/conflicts ---
app.get('/memory/conflicts', (req, res) => {
    try {
        const conflicts = db.prepare(`
            SELECT * FROM memories
            WHERE json_extract(metadata, '$.conflict') = 1
            AND scope != 'archived'
            ORDER BY updated_at DESC
        `).all();

        // Parse conflict details from metadata
        const parsed = conflicts.map(c => {
            let meta = {};
            try { meta = JSON.parse(c.metadata || '{}'); } catch (e) {}
            return {
                id: c.id,
                content: c.content,
                type: c.type,
                category: c.category,
                confidence: c.confidence,
                conflicts_with: meta.conflicts_with || null,
                detected_at: meta.conflict_detected_at || c.updated_at,
                status: 'unresolved',
            };
        });

        res.json({ conflicts: parsed });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// --- POST /memory/conflicts/resolve ---
app.post('/memory/conflicts/resolve', async (req, res) => {
    try {
        const { id, keep, reason } = req.body;
        if (!id || !keep) return res.status(400).json({ error: 'Missing id or keep (new|existing)' });

        const memory = stmts.getMemory.get(id);
        if (!memory) return res.status(404).json({ error: 'Memory not found' });

        let meta = {};
        try { meta = JSON.parse(memory.metadata || '{}'); } catch (e) {}

        await writeQueue.enqueue(() => {
            if (keep === 'existing') {
                // Delete the conflicting new memory, clear conflict flag
                delete meta.conflict;
                delete meta.conflicts_with;
                delete meta.conflict_detected_at;
                db.prepare(`UPDATE memories SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?`)
                    .run(JSON.stringify(meta), id);
                logChange('system', 'memories', 'update', id, { conflict: true }, { conflict: false }, `Conflict resolved: kept existing. ${reason || ''}`);
            } else if (keep === 'new') {
                // Boost new memory's confidence, archive the old conflicting one
                const newConf = Math.min(1.0, memory.confidence + 0.1);
                delete meta.conflict;
                delete meta.conflicts_with;
                delete meta.conflict_detected_at;
                db.prepare(`UPDATE memories SET confidence = ?, metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?`)
                    .run(newConf, JSON.stringify(meta), id);

                if (meta.conflicts_with) {
                    db.prepare(`UPDATE memories SET scope = 'archived', updated_at = CURRENT_TIMESTAMP WHERE id = ?`)
                        .run(meta.conflicts_with);
                    logChange('system', 'memories', 'update', meta.conflicts_with, { scope: 'global' }, { scope: 'archived' }, `Archived: conflict resolved in favor of ${id}`);
                }

                logChange('system', 'memories', 'update', id, { conflict: true }, { conflict: false, confidence: newConf }, `Conflict resolved: kept new. ${reason || ''}`);
            }
        });

        res.json({ resolved: true, id, kept: keep });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// --- POST /memory/promote ---
app.post('/memory/promote', async (req, res) => {
    try {
        // Find high-confidence memories not yet promoted
        const candidates = db.prepare(`
            SELECT * FROM memories
            WHERE confidence >= 0.8
            AND pinned = 0
            AND scope != 'archived'
            AND (metadata IS NULL OR json_extract(metadata, '$.promoted_to') IS NULL)
            ORDER BY confidence DESC
            LIMIT 50
        `).all();

        const promotionRules = [
            { type: 'procedural', categories: ['system', 'workflow'], file: 'TOOLS.md', section: 'Auto-Learned Rules' },
            { type: 'semantic', categories: ['user'], file: 'USER.md', section: 'Auto-Learned Preferences' },
            { type: 'semantic', categories: ['agent'], file: 'IDENTITY.md', section: 'Auto-Learned Self-Knowledge' },
            { type: 'procedural', categories: ['workflow'], file: 'AGENTS.md', section: 'Auto-Learned Workflow Rules' },
        ];

        const promoted = [];
        const skipped = [];

        for (const memory of candidates) {
            // Find matching promotion rule
            let rule = null;
            for (const r of promotionRules) {
                if (memory.type === r.type && r.categories.includes(memory.category)) {
                    rule = r;
                    break;
                }
            }
            if (!rule) {
                skipped.push({ id: memory.id, reason: 'no matching promotion rule' });
                continue;
            }

            // Determine which agents should receive this promotion
            const targetAgents = [];
            for (const [agentId, agentConf] of Object.entries(agentsConfig.agents)) {
                if (!agentConf.workspace) continue;

                // Check scope compatibility
                const scopes = getAgentScopes(agentId);
                if (scopes.includes(memory.scope)) {
                    targetAgents.push({ id: agentId, workspace: agentConf.workspace });
                }
            }

            for (const target of targetAgents) {
                const filePath = path.join(target.workspace, rule.file);

                // Read existing file or create
                let content = '';
                try { content = fs.readFileSync(filePath, 'utf8'); } catch (e) {}

                // Check if already present (fuzzy — check if content substring exists)
                if (content.includes(memory.content.substring(0, 50))) {
                    skipped.push({ id: memory.id, reason: 'already in file', file: rule.file, agent: target.id });
                    continue;
                }

                // Find or create the auto-learned section
                const sectionHeader = `## ${rule.section}`;
                const marker = '<!-- Auto-promoted from memory system. Review and edit as needed. -->';
                const entry = `- ${memory.content} (confidence: ${memory.confidence.toFixed(2)}, reinforced ${memory.access_count}x)`;

                if (content.includes(sectionHeader)) {
                    // Append to existing section
                    const idx = content.indexOf(sectionHeader);
                    const afterHeader = content.indexOf('\n', idx) + 1;
                    // Find end of section (next ## or end of file)
                    const nextSection = content.indexOf('\n## ', afterHeader);
                    const insertAt = nextSection !== -1 ? nextSection : content.length;
                    content = content.substring(0, insertAt) + entry + '\n' + content.substring(insertAt);
                } else {
                    // Append new section at end
                    content = content.trimEnd() + `\n\n${sectionHeader}\n${marker}\n${entry}\n`;
                }

                fs.writeFileSync(filePath, content);
                promoted.push({ id: memory.id, file: rule.file, agent: target.id, content: memory.content });
            }

            // Mark as promoted
            let meta = {};
            try { meta = JSON.parse(memory.metadata || '{}'); } catch (e) {}
            meta.promoted_to = rule.file;
            meta.promoted_at = new Date().toISOString();
            db.prepare(`UPDATE memories SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?`)
                .run(JSON.stringify(meta), memory.id);

            logChange('system', 'memories', 'update', memory.id,
                { promoted_to: null }, { promoted_to: rule.file },
                'Promoted to context file'
            );
        }

        res.json({ promoted, skipped: skipped.length, total_candidates: candidates.length });
    } catch (err) {
        console.error('Promote error:', err.message);
        res.status(500).json({ error: err.message });
    }
});

// --- POST /memory/search/semantic ---
app.post('/memory/search/semantic', async (req, res) => {
    try {
        const { query, scope, type, limit: limitStr } = req.body;
        if (!query) return res.status(400).json({ error: 'Missing query' });
        const limit = parseInt(limitStr) || 10;

        // Get all memories with embeddings
        let scopeFilter = '';
        let params = [];
        if (scope) {
            scopeFilter = 'AND scope = ?';
            params.push(scope);
        }
        if (type) {
            scopeFilter += ' AND type = ?';
            params.push(type);
        }

        const memories = db.prepare(`
            SELECT id, content, type, category, confidence, scope, embedding
            FROM memories
            WHERE scope != 'archived' AND embedding IS NOT NULL
            ${scopeFilter}
        `).all(...params);

        if (memories.length === 0) {
            return res.json({ results: [], method: 'none', note: 'No embedded memories found' });
        }

        // Embed only the query text (fast — single vector)
        const queryEmbBuf = await getEmbedding(query);
        if (!queryEmbBuf) {
            return res.status(503).json({ error: 'Embedding service unavailable' });
        }
        const queryVec = new Float32Array(queryEmbBuf.buffer, queryEmbBuf.byteOffset, queryEmbBuf.byteLength / 4);

        // Compute cosine similarity in-process against stored embeddings
        const scored = [];
        for (const m of memories) {
            if (!m.embedding) continue;
            const mVec = new Float32Array(m.embedding.buffer, m.embedding.byteOffset, m.embedding.byteLength / 4);
            let dot = 0;
            for (let i = 0; i < queryVec.length; i++) dot += queryVec[i] * mVec[i];
            scored.push({ id: m.id, content: m.content, type: m.type, category: m.category, confidence: m.confidence, scope: m.scope, similarity: dot });
        }

        // Sort by similarity descending, take top K
        scored.sort((a, b) => b.similarity - a.similarity);
        const results = scored.slice(0, limit);

        res.json({ results, method: 'embedding', total_embedded: memories.length });
    } catch (err) {
        console.error('Semantic search error:', err.message);
        res.status(500).json({ error: err.message });
    }
});

// --- POST /memory/dedup ---
app.post('/memory/dedup', async (req, res) => {
    try {
        const dryRun = req.query.dry !== 'false'; // default to dry run
        const threshold = parseFloat(req.query.threshold) || 0.80;

        // Get all active memories grouped by category
        const allMemories = db.prepare(`
            SELECT id, type, content, category, confidence, access_count, pinned, created_at, created_by
            FROM memories
            WHERE scope != 'archived'
            ORDER BY category, confidence DESC, access_count DESC
        `).all();

        // Group by category
        const catMap = {};
        for (const m of allMemories) {
            const cat = m.category || 'uncategorized';
            if (!catMap[cat]) catMap[cat] = [];
            catMap[cat].push(m);
        }

        const clusters = []; // groups of duplicates
        const toArchive = []; // IDs to archive (non-winners)

        for (const [cat, memories] of Object.entries(catMap)) {
            const used = new Set();

            for (let i = 0; i < memories.length; i++) {
                if (used.has(memories[i].id)) continue;

                const cluster = [memories[i]];
                used.add(memories[i].id);

                for (let j = i + 1; j < memories.length; j++) {
                    if (used.has(memories[j].id)) continue;
                    const sim = computeSimilarity(memories[i].content, memories[j].content);
                    if (sim >= threshold) {
                        cluster.push(memories[j]);
                        used.add(memories[j].id);
                    }
                }

                if (cluster.length > 1) {
                    // Winner: highest confidence, then most accessed, then newest
                    // (already sorted by confidence DESC, access_count DESC)
                    const winner = cluster[0];
                    const dupes = cluster.slice(1);
                    clusters.push({
                        category: cat,
                        winner: { id: winner.id, content: winner.content.substring(0, 100), confidence: winner.confidence },
                        duplicates: dupes.map(d => ({ id: d.id, content: d.content.substring(0, 100), confidence: d.confidence })),
                        count: cluster.length
                    });
                    for (const d of dupes) {
                        toArchive.push(d.id);
                    }
                }
            }
        }

        if (!dryRun && toArchive.length > 0) {
            const archiveStmt = db.prepare(`
                UPDATE memories SET scope = 'archived', updated_at = CURRENT_TIMESTAMP WHERE id = ?
            `);
            const archiveMany = db.transaction((ids) => {
                for (const id of ids) {
                    archiveStmt.run(id);
                    logChange('system', 'memories', 'update', id,
                        { scope: 'active' }, { scope: 'archived' }, 'Dedup: archived duplicate');
                }
            });
            archiveMany(toArchive);
        }

        res.json({
            dry_run: dryRun,
            threshold,
            total_memories: allMemories.length,
            duplicate_clusters: clusters.length,
            duplicates_found: toArchive.length,
            archived: dryRun ? 0 : toArchive.length,
            clusters: clusters.slice(0, 50), // cap output size
            note: dryRun ? 'Pass ?dry=false to actually archive duplicates' : 'Duplicates archived'
        });
    } catch (err) {
        console.error('Dedup error:', err.message);
        res.status(500).json({ error: err.message });
    }
});


// --- POST /memory/backfill-embeddings ---
app.post('/memory/backfill-embeddings', async (req, res) => {
    try {
        const batchSize = parseInt(req.query.batch) || 50;
        const limit = parseInt(req.query.limit) || 500;

        const unembedded = db.prepare(`
            SELECT id, content FROM memories
            WHERE embedding IS NULL AND scope != 'archived'
            ORDER BY created_at DESC
            LIMIT ?
        `).all(limit);

        if (unembedded.length === 0) {
            return res.json({ message: 'All memories already have embeddings', processed: 0 });
        }

        let processed = 0;
        const updateStmt = db.prepare('UPDATE memories SET embedding = ? WHERE id = ?');

        // Process in batches
        for (let i = 0; i < unembedded.length; i += batchSize) {
            const batch = unembedded.slice(i, i + batchSize);
            const texts = batch.map(m => m.content);
            const ids = batch.map(m => m.id);

            const result = await callEmbeddingService('/batch-embed', { texts, ids });
            if (!result || !result.results) continue;

            const tx = db.transaction(() => {
                for (const item of result.results) {
                    const embBuf = Buffer.from(item.embedding_b64, 'base64');
                    const mem = batch.find(m => m.id === item.id);
                    if (mem) {
                        updateStmt.run(embBuf, mem.id);
                        processed++;
                    }
                }
            });
            tx();
        }

        res.json({
            message: 'Backfill complete',
            processed,
            remaining: unembedded.length - processed,
            total_unembedded: unembedded.length,
        });
    } catch (err) {
        console.error('Backfill error:', err.message);
        res.status(500).json({ error: err.message });
    }
});

// --- CC Process Manager ---
const ccProcesses = new Map(); // taskId -> { process, startedAt, timeout }

function getActiveTaskCount() {
    let count = 0;
    for (const [, entry] of ccProcesses) {
        if (entry.process && !entry.process.killed) count++;
    }
    return count;
}

function spawnCCTask(taskId, task, context, mode, timeout, workspace) {
    const startedAt = Date.now();
    const timeoutMs = (timeout || 300) * 1000;

    // Build the prompt with context injection
    let prompt = task;
    if (context) {
        prompt = `Context:\n${context}\n\nTask:\n${task}`;
    }

    // Track as active task (no process object, but track for concurrency limits)
    const entry = { process: null, startedAt, timer: null };
    ccProcesses.set(taskId, entry);

    // Call Anthropic API directly (async)
    callAnthropic(prompt, { timeoutMs })
        .then((output) => {
            const duration = Math.floor((Date.now() - startedAt) / 1000);
            db.prepare(`
                UPDATE tasks
                SET status = ?, output = ?, completed_at = CURRENT_TIMESTAMP,
                    last_heartbeat = CURRENT_TIMESTAMP
                WHERE id = ?
            `).run('completed', output, taskId);

            logChange('system', 'tasks', 'update', taskId,
                { status: 'in_progress' },
                { status: 'completed', duration },
                `Task finished (${duration}s)`
            );
            ccProcesses.delete(taskId);
        })
        .catch((err) => {
            const duration = Math.floor((Date.now() - startedAt) / 1000);
            db.prepare(`
                UPDATE tasks
                SET status = 'failed', output = ?, completed_at = CURRENT_TIMESTAMP,
                    last_heartbeat = CURRENT_TIMESTAMP
                WHERE id = ?
            `).run(`API error: ${err.message}`, taskId);

            logChange('system', 'tasks', 'update', taskId,
                { status: 'in_progress' },
                { status: 'failed', duration },
                `Task failed: ${err.message}`
            );
            ccProcesses.delete(taskId);
        });
}


// --- Session State ---
// Agents self-report their active task state. One entry per agent, overwritten each time.

app.post('/memory/session-state', async (req, res) => {
    try {
        const { agent, task_summary, status, context, channel } = req.body;
        if (!agent) return res.status(400).json({ error: 'Missing agent field' });
        if (!task_summary) return res.status(400).json({ error: 'Missing task_summary field' });

        const content = JSON.stringify({ task_summary, status: status || 'active', context: context || null, channel: channel || null });
        const scope = channel ? `agent:${agent}:channel:${channel}` : `agent:${agent}`;

        // Check if session state already exists for this agent (+channel)
        const existing = db.prepare(`
            SELECT id FROM memories
            WHERE type = 'episodic' AND scope = ? AND category = 'session'
        `).get(scope);

        if (existing) {
            // Overwrite existing
            await writeQueue.enqueue(() => {
                db.prepare(`
                    UPDATE memories SET content = ?, updated_at = CURRENT_TIMESTAMP, last_accessed = CURRENT_TIMESTAMP
                    WHERE id = ?
                `).run(content, existing.id);
                logChange(agent, 'memories', 'update', existing.id,
                    { content: '(previous state)' },
                    { content },
                    'Session state updated'
                );
            });
            res.json({ id: existing.id, action: 'updated' });
        } else {
            // Create new
            const id = `session_${agent}_${Date.now()}`;
            await writeQueue.enqueue(() => {
                db.prepare(`
                    INSERT INTO memories (id, type, content, created_by, scope, confidence, category, tags, pinned)
                    VALUES (?, 'episodic', ?, ?, ?, 1.0, 'session', '["session-state","active-task"]', 0)
                `).run(id, content, agent, scope);
                logChange(agent, 'memories', 'insert', id, null, { content }, 'Session state created');
            });
            res.json({ id, action: 'created' });
        }
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

app.get('/memory/session-state/:agent', (req, res) => {
    try {
        const agent = req.params.agent;
        const { channel } = req.query;
        const scope = channel ? `agent:${agent}:channel:${channel}` : `agent:${agent}`;

        let state = db.prepare(`
            SELECT id, content, updated_at, last_accessed FROM memories
            WHERE type = 'episodic' AND scope = ? AND category = 'session'
        `).get(scope);

        // Fallback to agent-level state if no channel-specific state
        if (!state && channel) {
            state = db.prepare(`
                SELECT id, content, updated_at, last_accessed FROM memories
                WHERE type = 'episodic' AND scope = ? AND category = 'session'
            `).get(`agent:${agent}`);
        }

        if (!state) {
            return res.json({ agent, channel: channel || null, state: null, message: 'No session state saved for this agent' });
        }

        let parsed;
        try { parsed = JSON.parse(state.content); } catch { parsed = state.content; }

        res.json({
            agent,
            channel: channel || null,
            id: state.id,
            state: parsed,
            updated_at: state.updated_at,
            last_accessed: state.last_accessed
        });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});


// --- GET /sessions/history ---
const SESSION_INDEX_PATH = path.join(__dirname, 'session-index.json');

app.get('/sessions/history', async (req, res) => {
    try {
        const { channel, agent, after, before, limit: limitStr, summary } = req.query;
        if (!channel) return res.status(400).json({ error: 'Missing channel parameter' });

        const limit = parseInt(limitStr) || 50;

        // Resolve relative times
        const resolveTime = (val) => {
            if (!val) return null;
            const match = val.match(/^(\d+)([dhm])$/);
            if (match) {
                const n = parseInt(match[1]);
                const ms = { d: 86400000, h: 3600000, m: 60000 }[match[2]];
                return new Date(Date.now() - n * ms).toISOString();
            }
            return val;
        };
        const afterTime = resolveTime(after);
        const beforeTime = resolveTime(before);

        // Load session index
        let index = {};
        try {
            index = JSON.parse(fs.readFileSync(SESSION_INDEX_PATH, 'utf8'));
        } catch {
            return res.status(500).json({ error: 'Session index not found. Run extraction cron first.' });
        }

        // Filter sessions by channel + agent + date range
        const matchingSessions = Object.entries(index).filter(([sid, info]) => {
            if (info.channel !== channel && channel !== 'all') return false;
            if (agent && info.agent !== agent) return false;
            if (afterTime && info.end_time && info.end_time < afterTime) return false;
            if (beforeTime && info.start_time && info.start_time > beforeTime) return false;
            return true;
        });

        if (matchingSessions.length === 0) {
            return res.json({ channel, message_count: 0, messages: [], sessions_scanned: 0 });
        }

        // Read messages from matching session files
        const messages = [];
        let sessionsScanned = 0;

        for (const [sid, info] of matchingSessions) {
            if (!info.file || !fs.existsSync(info.file)) continue;
            sessionsScanned++;

            const lines = fs.readFileSync(info.file, 'utf8').split('\n');
            for (const line of lines) {
                if (!line.trim()) continue;
                let d;
                try { d = JSON.parse(line); } catch { continue; }

                // Skip non-messages and compaction entries
                if (d.type === 'compaction') continue;
                if (d.type !== 'message') continue;

                const msg = d.message || {};
                const role = msg.role;
                if (role !== 'user' && role !== 'assistant') continue;

                const ts = d.timestamp;
                if (afterTime && ts < afterTime) continue;
                if (beforeTime && ts > beforeTime) continue;

                // Extract text content
                let content = msg.content || '';
                if (Array.isArray(content)) {
                    content = content
                        .filter(b => b && b.type === 'text')
                        .map(b => b.text || '')
                        .join('\n');
                }

                // Skip empty or very short
                if (content.length < 10) continue;

                // Truncate very long messages
                if (content.length > 2000) content = content.substring(0, 2000) + '...';

                messages.push({ timestamp: ts, role, content, session_id: sid });
            }
        }

        // Sort by timestamp descending (newest first)
        messages.sort((a, b) => b.timestamp.localeCompare(a.timestamp));

        // Apply limit
        const limited = messages.slice(0, limit);

        // Summary mode
        if (summary === 'true' && limited.length > 0) {
            const conversationText = limited.map(m => `${m.role}: ${m.content}`).join('\n---\n');
            const summaryPrompt = `Summarize the topics discussed in this conversation in bullet points. Be concise.\n\nConversation:\n${conversationText}`;
            try {
                const summaryOutput = await callAnthropic(summaryPrompt, { timeoutMs: 60000 });
                return res.json({
                    channel,
                    mode: 'summary',
                    message_count: limited.length,
                    sessions_scanned: sessionsScanned,
                    date_range: {
                        from: limited[limited.length - 1]?.timestamp,
                        to: limited[0]?.timestamp
                    },
                    summary: summaryOutput.trim()
                });
            } catch (err) {
                return res.status(500).json({ error: `Summary generation failed: ${err.message}` });
            }
        }

        res.json({
            channel,
            message_count: limited.length,
            sessions_scanned: sessionsScanned,
            date_range: {
                from: limited.length ? limited[limited.length - 1].timestamp : null,
                to: limited.length ? limited[0].timestamp : null
            },
            messages: limited
        });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});


// --- POST /task ---
app.post('/task', async (req, res) => {
    try {
        const { task, context, mode, timeout, workspace, requested_by } = req.body;
        if (!task) return res.status(400).json({ error: 'Missing task field' });
        if (!requested_by) return res.status(400).json({ error: 'Missing requested_by field' });

        // Check concurrency
        const active = getActiveTaskCount();
        if (active >= MAX_CC_CONCURRENT) {
            return res.status(429).json({ status: 'rejected', reason: 'max_concurrent_reached', active, max: MAX_CC_CONCURRENT });
        }

        const taskId = uuidv4();

        // Inject relevant memory context
        let enrichedContext = context || '';
        try {
            const memories = db.prepare(`
                SELECT m.content, m.type, m.confidence
                FROM memories_fts f
                JOIN memories m ON m.rowid = f.rowid
                WHERE memories_fts MATCH ?
                AND m.scope != 'archived'
                ORDER BY rank
                LIMIT 10
            `).all(task.substring(0, 200).replace(/[^a-zA-Z0-9\s]/g, ' ').trim().split(/\s+/).filter(Boolean).map(w => `"${w}"`).join(' OR '));

            if (memories.length > 0) {
                const memoryBlock = memories.map(m => `[${m.type}|${m.confidence}] ${m.content}`).join('\n');
                enrichedContext = `Relevant memories:\n${memoryBlock}\n\n${enrichedContext}`;
            }
        } catch (e) {
            // FTS match can fail on special chars — non-fatal
        }

        // Create task record
        await writeQueue.enqueue(() => {
            stmts.insertTask.run(
                taskId, task.substring(0, 200), task,
                'in_progress', 3, null, requested_by,
                JSON.stringify({ mode: mode || 'one-shot', workspace, timeout: timeout || 300 }),
                null
            );
            logChange(requested_by, 'tasks', 'insert', taskId, null,
                { task: task.substring(0, 200), mode: mode || 'one-shot' },
                'CC task spawned'
            );
            // Update started_at
            db.prepare(`UPDATE tasks SET started_at = CURRENT_TIMESTAMP, assigned_to = 'claude-cli' WHERE id = ?`).run(taskId);
        });

        // Spawn async — don't await, return immediately
        spawnCCTask(taskId, task, enrichedContext, mode || 'one-shot', timeout || 300, workspace);

        res.json({ task_id: taskId, status: 'running', active_tasks: active + 1 });
    } catch (err) {
        console.error('Task spawn error:', err.message);
        res.status(500).json({ error: err.message });
    }
});

// --- GET /task/:id ---
app.get('/task/:id', (req, res) => {
    try {
        const task = stmts.getTask.get(req.params.id);
        if (!task) return res.status(404).json({ error: 'Task not found' });

        const isRunning = ccProcesses.has(req.params.id);
        const duration = isRunning
            ? Math.floor((Date.now() - ccProcesses.get(req.params.id).startedAt) / 1000)
            : null;

        res.json({
            task_id: task.id,
            title: task.title,
            status: isRunning ? 'running' : task.status,
            output: task.output,
            created_by: task.created_by,
            created_at: task.created_at,
            started_at: task.started_at,
            completed_at: task.completed_at,
            duration_seconds: duration,
            tokens_used: task.tokens_used,
            cost_usd: task.cost_usd,
        });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// --- GET /tasks ---
app.get('/tasks', (req, res) => {
    try {
        const { status } = req.query;
        let sql = 'SELECT id, title, status, created_by, assigned_to, created_at, started_at, completed_at FROM tasks';
        const params = [];

        if (status && status !== 'all') {
            sql += ' WHERE status = ?';
            params.push(status);
        }
        sql += ' ORDER BY created_at DESC LIMIT 50';

        const tasks = db.prepare(sql).all(...params);
        res.json({
            tasks,
            active_cc_processes: getActiveTaskCount(),
            max_concurrent: MAX_CC_CONCURRENT,
        });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// --- DELETE /task/:id ---
app.delete('/task/:id', (req, res) => {
    try {
        const entry = ccProcesses.get(req.params.id);
        if (!entry) {
            // Check if task exists in DB
            const task = stmts.getTask.get(req.params.id);
            if (!task) return res.status(404).json({ error: 'Task not found' });
            return res.json({ killed: false, reason: 'Task already finished', status: task.status });
        }

        clearTimeout(entry.timer);
        entry.process.kill('SIGTERM');
        setTimeout(() => {
            if (entry.process && !entry.process.killed) entry.process.kill('SIGKILL');
        }, 5000);

        db.prepare(`UPDATE tasks SET status = 'failed', output = 'Killed by user', completed_at = CURRENT_TIMESTAMP WHERE id = ?`)
            .run(req.params.id);

        logChange('system', 'tasks', 'update', req.params.id,
            { status: 'in_progress' }, { status: 'failed' }, 'Killed by user request');

        ccProcesses.delete(req.params.id);

        res.json({ killed: true, task_id: req.params.id });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// --- Start ---
const server = app.listen(PORT, BIND_HOST, () => {
    console.log(`Agent Services API running on ${BIND_HOST}:${PORT}`);
    sendOpsAlert('✅ **Broker restarted** — agent-services up on port ' + PORT);
    console.log(`Database: ${DB_PATH}`);
    console.log(`Agents: ${Object.keys(agentsConfig.agents).join(', ')}`);
    // Check embedding service health after startup
    setTimeout(() => {
        const result = callEmbeddingServiceSync('/health', {});
        if (result && result.status === 'ok') {
            console.log('Embedding service healthy:', result.model);
        } else {
            console.error('WARNING: Embedding service not responding');
            sendOpsAlert('⚠️ **Embedding service not responding** — semantic search/dedup will fall back to Jaccard');
        }
    }, 10000);
});

// Graceful shutdown
function gracefulShutdown(signal) {
    console.log(`${signal} received, shutting down...`);
    // Kill all running CC processes
    for (const [taskId, entry] of ccProcesses) {
        console.log(`Killing CC task ${taskId}`);
        clearTimeout(entry.timer);
        entry.process.kill('SIGTERM');
        db.prepare(`UPDATE tasks SET status = 'failed', output = 'Server shutdown', completed_at = CURRENT_TIMESTAMP WHERE id = ?`)
            .run(taskId);
    }
    ccProcesses.clear();
    server.close(() => {
        db.close();
        process.exit(0);
    });
}

process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
process.on('SIGINT', () => gracefulShutdown('SIGINT'));
