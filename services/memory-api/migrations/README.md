# Memory DB Migrations

Place versioned SQL migration files here. They run automatically at startup.

Format: `NNN_description.sql` (e.g., `001_initial_schema.sql`)

The migration runner checks `schema_version` table and applies pending migrations in order.
