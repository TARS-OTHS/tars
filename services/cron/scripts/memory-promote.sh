#!/bin/bash
echo "Running promotion at $(date)"
curl -s -X POST ${MEMORY_API_URL:-http://memory-api:8897}/memory/promote
echo ""
echo "Promotion completed at $(date)"
