#!/bin/bash
set -e

echo "🚀 Starting On-call Assistant Frontend..."

cd frontend

echo "📦 Installing dependencies if needed..."
npm install --silent

echo "⚡️ Starting Vite dev server..."
npm run dev
