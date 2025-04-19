#!/bin/bash

# Make the script executable
chmod +x start.sh

# Install Chrome for Selenium
apt-get update && apt-get install -y wget gnupg2 apt-utils unzip

# Add Chrome repository
wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -
echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list

# Install Chrome
apt-get update && apt-get install -y google-chrome-stable

# Create data directory
mkdir -p data

# Start the application with gunicorn for production
gunicorn -w 1 -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:$PORT --timeout 120
