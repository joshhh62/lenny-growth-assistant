#!/usr/bin/env bash
# End-to-end smoke test against a running stack (docker compose up, or dev servers).
# Usage: scripts/smoke.sh [API_BASE]      default http://localhost:8000
set -euo pipefail
API="${1:-http://localhost:8000}"
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }
need() { command -v "$1" >/dev/null || fail "missing dependency: $1"; }
need curl; need python3

echo "1. Liveness"
curl -fsS "$API/health" >/dev/null && pass "GET /health"

echo "2. Readiness"
READY=$(curl -fsS "$API/health/ready")
python3 - "$READY" <<'EOF'
import json, sys
d = json.loads(sys.argv[1])
print(f"     database ok={d['database']['ok']} episodes={d['database'].get('episodes')} chunks={d['database'].get('chunks')} embedded={d['database'].get('embedded')}")
for p in d["providers"]:
    print(f"     provider {p['name']:<10} available={p['available']!s:<5} model={p['model']} runtime={p['runtime']}  {p['reason'] if not p['available'] else ''}")
if not d["database"]["ok"]:
    sys.exit("database not reachable")
if not any(p["available"] for p in d["providers"]):
    sys.exit("no LLM provider available (start Ollama or set ANTHROPIC_API_KEY)")
EOF
pass "GET /health/ready"

echo "3. Knowledge base"
EP=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['database']['episodes'])" "$READY")
if [ "$EP" -eq 0 ]; then
  echo "     empty — waiting for first ingest (clones ~28 MB of transcripts)…"
  for i in $(seq 1 60); do
    sleep 5
    EP=$(curl -fsS "$API/health/ready" | python3 -c "import json,sys; print(json.load(sys.stdin)['database']['episodes'])")
    [ "$EP" -gt 0 ] && break
  done
fi
[ "$EP" -gt 0 ] && pass "$EP episodes indexed" || fail "ingest did not complete"

echo "4. Retrieval"
HITS=$(curl -fsS "$API/api/search?q=retention%20growth&k=3" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['hits']), d['mode'])")
pass "search returned $HITS"

echo "5. Session + grounded chat (streams; may take a minute on a CPU model)"
SID=$(curl -fsS -X POST "$API/api/sessions" -H 'content-type: application/json' -d '{"user_id":"smoke"}' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
pass "session $SID"
OUT=$(curl -sS -N --max-time 300 -X POST "$API/api/sessions/$SID/messages" -H 'content-type: application/json' \
      -d '{"content":"In one paragraph: what does Elena Verna say about retention?"}')
echo "$OUT" | grep -q '^event: provider' && pass "provider event"
echo "$OUT" | grep -q '^event: citations' && pass "citations event"
echo "$OUT" | grep -q '^event: token' && pass "streamed tokens"
if echo "$OUT" | grep -q '^event: error'; then
  echo "$OUT" | grep -A1 '^event: error' | tail -1; fail "turn ended with an error"
fi
echo "$OUT" | grep -q '^event: done' && pass "done event"

echo "6. Persistence"
N=$(curl -fsS "$API/api/sessions/$SID" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['messages']), len(d['messages'][-1]['citations']))")
pass "messages stored (count, citations) = $N"

echo "7. Structured errors"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$API/api/sessions/00000000-0000-0000-0000-000000000000")
[ "$CODE" = "404" ] && pass "404 for unknown session" || fail "expected 404, got $CODE"

curl -fsS -X DELETE "$API/api/sessions/$SID" >/dev/null && pass "cleanup"
echo "All good."
