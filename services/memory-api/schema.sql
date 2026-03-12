-- Enable WAL mode for concurrent reads
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;

-- ============================================
-- MEMORIES: Core memory with confidence lifecycle
-- ============================================
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('semantic', 'episodic', 'procedural')),
    content TEXT NOT NULL,
    category TEXT,
    confidence REAL DEFAULT 0.5,
    access_count INTEGER DEFAULT 0,
    last_accessed DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT,
    scope TEXT DEFAULT 'global',
    scope_target TEXT,
    tags TEXT,
    metadata TEXT,
    pinned INTEGER DEFAULT 0,
    embedding BLOB
);

CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, scope_target);
CREATE INDEX IF NOT EXISTS idx_memories_confidence ON memories(confidence);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);

-- Full-text search index
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, category, tags,
    content='memories',
    content_rowid='rowid'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags)
    VALUES (new.rowid, new.content, new.category, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
    VALUES('delete', old.rowid, old.content, old.category, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
    VALUES('delete', old.rowid, old.content, old.category, old.tags);
    INSERT INTO memories_fts(rowid, content, category, tags)
    VALUES (new.rowid, new.content, new.category, new.tags);
END;

-- ============================================
-- RELATIONSHIPS: Lightweight entity graph
-- ============================================
CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,
    entity_a TEXT NOT NULL,
    relationship TEXT NOT NULL,
    entity_b TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    created_by TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rel_entities ON relationships(entity_a, entity_b);

-- ============================================
-- TASKS: Task queue and tracking
-- ============================================
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'assigned', 'in_progress', 'review', 'completed', 'failed', 'stale')),
    priority INTEGER DEFAULT 3 CHECK(priority BETWEEN 1 AND 5),
    assigned_to TEXT,
    created_by TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME,
    completed_at DATETIME,
    last_heartbeat DATETIME,
    context TEXT,
    output TEXT,
    parent_task TEXT REFERENCES tasks(id),
    tags TEXT,
    model_used TEXT,
    tokens_used INTEGER,
    cost_usd REAL,
    escalation_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_to);

-- ============================================
-- TASK REVIEWS: Structured self-improvement
-- ============================================
CREATE TABLE IF NOT EXISTS task_reviews (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id),
    agent_id TEXT NOT NULL,
    success INTEGER NOT NULL,
    time_taken_seconds INTEGER,
    model_used TEXT,
    estimated_complexity TEXT CHECK(estimated_complexity IN ('trivial', 'simple', 'medium', 'complex', 'very_complex')),
    actual_complexity TEXT CHECK(actual_complexity IN ('trivial', 'simple', 'medium', 'complex', 'very_complex')),
    what_failed TEXT,
    what_to_repeat TEXT,
    routing_suggestion TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- TRACKING: Food, training, health etc
-- ============================================
CREATE TABLE IF NOT EXISTS tracking (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('food', 'training', 'sleep', 'weight', 'other')),
    date DATE NOT NULL,
    time TIME,
    data TEXT NOT NULL,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracking_type_date ON tracking(type, date);

-- ============================================
-- PEOPLE: Contacts and relationships
-- ============================================
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    relationship TEXT,
    context TEXT,
    notes TEXT,
    contact_info TEXT,
    metadata TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- PROJECTS: Active workstreams
-- ============================================
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'paused', 'completed', 'archived')),
    description TEXT,
    links TEXT,
    notes TEXT,
    metadata TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- CHANGELOG: Full audit trail
-- ============================================
CREATE TABLE IF NOT EXISTS changelog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    agent TEXT NOT NULL,
    table_name TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('insert', 'update', 'delete')),
    record_id TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_changelog_table ON changelog(table_name, timestamp);
CREATE INDEX IF NOT EXISTS idx_changelog_record ON changelog(record_id);
