#!/bin/bash
# Create required directories
mkdir -p data
mkdir -p match_logs
mkdir -p debug_html

# Set environment variable for Render detection
export RENDER=true

# Set environment variables for better performance
export PYTHONUNBUFFERED=1
export WEB_CONCURRENCY=1  # Ensure only one worker to avoid multiple scrapers

# Increase timeout settings for the web requests
export SELENIUM_TIMEOUT=60
export PLAYWRIGHT_TIMEOUT=60

# Install requirements first to ensure we have all needed packages
echo "Installing Python dependencies..."
pip install -U fastapi uvicorn selenium pydantic python-multipart requests beautifulsoup4 pytz aiofiles webdriver-manager playwright

# Initialize Playwright (if installed)
if command -v playwright &> /dev/null; then
    echo "Installing Playwright browsers..."
    playwright install chromium
    echo "Playwright browsers installed"
else
    echo "Playwright not available, will rely on Selenium only"
fi

# Increase system limits
ulimit -n 4096 || echo "Failed to increase file descriptor limit (not critical)"

# Start the application with proper logging and extended timeouts
echo "Starting application on port $PORT"
exec uvicorn app:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 300 --log-level info
