#!/bin/bash
set -e

echo "🚀 Starting On-call Assistant Backend..."

cd backend
source ../venv/bin/activate

echo "📦 Installing dependencies if needed..."
pip install -q -r requirements.txt

echo "🔥 Starting FastAPI server..."
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
