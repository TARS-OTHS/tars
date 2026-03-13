# MEMORY.md — Persistent Memory System

You have a persistent memory database. It survives session resets. Use it.

## Architecture

- **Memory API**: `http://172.17.0.1:8897`
- **Embedding service**: `http://172.17.0.1:8896` (BGE-small-en-v1.5, 384-dim, ONNX)
- **Storage**: SQLite + FTS5 full-text search + vector embeddings
- **Types**: semantic, episodic, procedural
- **Categories**: system, project, episodic, user, business, people, infrastructure, procedural, session, agent

## Quick Reference

### Search memories (full-text)
```
GET http://DOCKER_HOST_IP:8897/memory/search?q=<query>&agent=main&limit=10
```
Optional params: `type`, `scope`, `after` (e.g. "7d", "2h"), `before`, `sort` ("time" or rank)

### Semantic search (embedding similarity)
```
POST http://DOCKER_HOST_IP:8897/memory/search/semantic
{ "query": "what to find", "limit": 10 }
```

### Store a memory
```
POST http://DOCKER_HOST_IP:8897/memory/write
{
  "table": "memories",
  "action": "insert",
  "agent": "main",
  "data": {
    "content": "thing to remember",
    "type": "semantic",
    "category": "general",
    "confidence": 0.8
  }
}
```

### Save session state (do this on task transitions)
```
POST http://DOCKER_HOST_IP:8897/memory/session-state
{
  "agent": "main",
  "task_summary": "what we were doing",
  "status": "in_progress",
  "context": "key details and next steps"
}
```

### Get session state (do this FIRST every session)
```
GET http://DOCKER_HOST_IP:8897/memory/session-state/main
```

### Get context (pinned + recent + conflicts + tasks)
```
GET http://DOCKER_HOST_IP:8897/memory/context?agent=main
```

### Pin a memory (never decays)
```
POST http://DOCKER_HOST_IP:8897/memory/write
{
  "table": "memories",
  "action": "update",
  "data": { "id": "<memory-id>", "pinned": true }
}
```

## All Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | /status | System health and stats |
| GET | /memory/search | Full-text search |
| POST | /memory/search/semantic | Embedding-based search |
| GET | /memory/context | Pinned, recent, conflicts, tasks |
| POST | /memory/write | Insert/update/delete across all tables |
| GET | /memory/query | Arbitrary SELECT queries |
| POST | /memory/extract | Extract facts from conversation text |
| GET | /memory/relationships | Entity relationship graph |
| GET | /memory/entities | All entities with connection counts |
| GET | /memory/conflicts | Unresolved conflicts |
| POST | /memory/conflicts/resolve | Resolve a conflict |
| POST | /memory/promote | Promote high-confidence to workspace |
| POST | /memory/dedup | Find/archive duplicates |
| POST | /memory/backfill-embeddings | Generate missing embeddings |
| POST | /memory/session-state | Save agent session state |
| GET | /memory/session-state/:agent | Get agent session state |
| GET | /sessions/history | Query session history |
| POST | /task | Spawn async code execution task |
| GET | /task/:id | Get task status/output |
| GET | /tasks | List all tasks |
| DELETE | /task/:id | Kill a running task |

## Write API Tables

The `/memory/write` endpoint supports these tables:
- **memories** — core memory storage
- **relationships** — entity relationships (entity_a, relationship, entity_b)
- **tasks** — task tracking (title, description, status, priority)
- **tracking** — general tracking data
- **people** — people database (name, relationship, context, contact_info)
- **projects** — project tracking (name, status, description)

## Memory Lifecycle

- **Confidence decay**: Memories not accessed for 7+ days decay gradually
- **Archival**: Confidence <= 0.1 AND 60 days unaccessed -> archived
- **Purge**: Archived > 30 days -> deleted
- **Pinned memories**: Never decay, never archive, never purge
- **Dedup**: Duplicates (>= 0.80 similarity) -> lower quality archived

## Rules

- You ARE a persistent agent. You have continuity across sessions via this system.
- Never say you have "no memory" or "can't remember" — search first.
- Save session state on every task transition.
- Read session state at session start.
