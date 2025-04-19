#!/bin/bash

# Create necessary directories
mkdir -p data debug

# Print Chrome version for debugging
echo "Chrome version:"
google-chrome --version

# Start the application with gunicorn for production
gunicorn -w 1 -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:$PORT --timeout 120
