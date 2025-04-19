#!/bin/bash

# Make the script executable
chmod +x start.sh

# Install Chrome for Selenium
apt-get update
apt-get install -y wget gnupg2 apt-utils
wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -
echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list
apt-get update
apt-get install -y google-chrome-stable

# Create data directory if it doesn't exist
mkdir -p data

# Start the application with gunicorn
gunicorn -w 1 -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:$PORT
