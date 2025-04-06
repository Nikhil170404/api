#!/bin/bash

# Create data directory
mkdir -p data

# Set environment variable for Render detection
export RENDER=true

# Start the FastAPI app with uvicorn
uvicorn app:app --host 0.0.0.0 --port $PORT
