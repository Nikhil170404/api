#!/bin/bash
set -e

echo "Starting deployment process..."

# Install Chrome and dependencies for Render environment
apt-get update
apt-get install -y wget gnupg
wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -
echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list
apt-get update
apt-get install -y google-chrome-stable

echo "Chrome installed successfully"

# Set display port for Selenium
export DISPLAY=:99

# Tell Render to use this script instead of its default
if [ -z "${PORT}" ]; then
  PORT=8000
fi
echo "Using port: $PORT"

# Direct execution of the python script - no gunicorn
echo "Starting application directly with Python..."
exec python app.py
