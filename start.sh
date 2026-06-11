#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "🚀 Starting Outbound Mass Caller..."

if [ -f ".env" ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

echo "📋 Configuration:"
echo "   LiveKit:  ${LIVEKIT_URL}"
echo "   Gemini:   ${GEMINI_MODEL:-gemini-3.1-flash-live-preview}"
echo "   Supabase: ${SUPABASE_URL}"
echo "   Trunk:    ${OUTBOUND_TRUNK_ID}"

# ── LiveKit agent worker (places the actual SIP calls) ───────────────────────
# Runs in background with auto-restart so a worker crash never takes down the
# dashboard (which is what Coolify health-checks on port 8000). If the worker
# keeps restarting, its logs below tell you exactly why.
(
  while true; do
    echo "🤖 [worker] Starting LiveKit agent worker (outbound-caller)..."
    python agent.py start || echo "🤖 [worker] exited (code $?) — restarting in 5s"
    sleep 5
  done
) &

# ── FastAPI dashboard (foreground → keeps the container alive) ────────────────
echo "🌐 Starting FastAPI server on port 8000..."
exec uvicorn server:app --host 0.0.0.0 --port 8000 --log-level info
