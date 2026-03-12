// Semantic dedup — clusters all memories by embedding cosine similarity
// Archives duplicates, keeps the highest-confidence/most-accessed winner per cluster

const Database = require('better-sqlite3');
const db = new Database('${TARS_HOME}/agent-services/memory.db');

const THRESHOLD = 0.80;
const DRY_RUN = process.argv.includes('--dry');

function cosine(a, b) {
    const va = new Float32Array(a.buffer, a.byteOffset, a.byteLength / 4);
    const vb = new Float32Array(b.buffer, b.byteOffset, b.byteLength / 4);
    let dot = 0;
    for (let i = 0; i < va.length; i++) dot += va[i] * vb[i];
    return dot;
}

// Get all active memories with embeddings
const memories = db.prepare(`
    SELECT id, content, type, category, confidence, access_count, pinned, created_at, embedding
    FROM memories
    WHERE scope != 'archived' AND embedding IS NOT NULL
    ORDER BY confidence DESC, access_count DESC, created_at DESC
`).all();

console.log(`Total memories with embeddings: ${memories.length}`);
console.log(`Threshold: ${THRESHOLD}`);
console.log(`Mode: ${DRY_RUN ? 'DRY RUN' : 'LIVE'}`);
console.log();

// Cluster by cosine similarity
const used = new Set();
const clusters = [];

for (let i = 0; i < memories.length; i++) {
    if (used.has(memories[i].id)) continue;

    const cluster = [memories[i]];
    used.add(memories[i].id);

    for (let j = i + 1; j < memories.length; j++) {
        if (used.has(memories[j].id)) continue;

        const sim = cosine(memories[i].embedding, memories[j].embedding);
        if (sim >= THRESHOLD) {
            cluster.push(memories[j]);
            used.add(memories[j].id);
        }
    }

    if (cluster.length > 1) {
        clusters.push(cluster);
    }
}

console.log(`Found ${clusters.length} duplicate clusters`);

let totalArchived = 0;
const archiveStmt = db.prepare(`UPDATE memories SET scope = 'archived', updated_at = CURRENT_TIMESTAMP WHERE id = ?`);
const logStmt = db.prepare(`INSERT INTO changelog (agent, table_name, action, record_id, old_value, new_value, reason) VALUES (?, ?, ?, ?, ?, ?, ?)`);

const archiveTx = db.transaction((toArchive, winnerId) => {
    for (const m of toArchive) {
        archiveStmt.run(m.id);
        logStmt.run('system', 'memories', 'update', m.id,
            JSON.stringify({ scope: 'active' }),
            JSON.stringify({ scope: 'archived' }),
            `Semantic dedup: archived (winner: ${winnerId})`);
    }
});

for (const cluster of clusters) {
    // Winner is first (already sorted by confidence DESC, access_count DESC)
    // But prefer pinned memories as winners
    cluster.sort((a, b) => {
        if (a.pinned !== b.pinned) return b.pinned - a.pinned;
        if (Math.abs(a.confidence - b.confidence) > 0.05) return b.confidence - a.confidence;
        if (a.access_count !== b.access_count) return b.access_count - a.access_count;
        // Prefer longer, more detailed content
        return b.content.length - a.content.length;
    });

    const winner = cluster[0];
    const dupes = cluster.slice(1);

    console.log(`
Cluster (${cluster.length} memories):`);
    console.log(`  KEEP: [${winner.confidence}] ${winner.content.substring(0, 100)}`);
    for (const d of dupes) {
        const sim = cosine(winner.embedding, d.embedding);
        console.log(`  DROP: [${d.confidence}] (sim=${sim.toFixed(3)}) ${d.content.substring(0, 80)}`);
    }

    if (!DRY_RUN) {
        archiveTx(dupes, winner.id);
        totalArchived += dupes.length;
    } else {
        totalArchived += dupes.length;
    }
}

console.log(`
=== Summary ===`);
console.log(`Clusters: ${clusters.length}`);
console.log(`${DRY_RUN ? 'Would archive' : 'Archived'}: ${totalArchived} memories`);
console.log(`Remaining active: ${memories.length - totalArchived}`);

db.close();
