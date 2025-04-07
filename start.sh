#!/bin/bash
# Enhanced startup script for free Render deployment

# Create required directories
mkdir -p data
mkdir -p logs

# Set environment variables for performance tuning
export PYTHONUNBUFFERED=1
export WEB_CONCURRENCY=1  # Single worker process for consistency
export MAX_CLIENTS_PER_MINUTE=2000  # Higher limit for production
export SCRAPE_INTERVAL=1  # Keep 1-second scraping interval

# Configure memory settings for better performance
export PYTHONMALLOC=malloc
export MALLOC_ARENA_MAX=2  # Limit memory fragmentation

# System tuning for render
ulimit -n 4096  # Increase file descriptor limit
echo "Setting system limits for high concurrency"

# Install required packages directly (in case requirements.txt is not used)
pip install fastapi uvicorn aiohttp beautifulsoup4 cachetools

# Log startup details
echo "Starting Cricket Odds API on port $PORT"
echo "Memory optimization enabled"
echo "Rate limiting set to $MAX_CLIENTS_PER_MINUTE requests per minute"

# Start the application with proper settings for high concurrency
exec uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1 --loop uvloop --http httptools --log-level info --timeout-keep-alive 75
