#!/bin/bash

# Make the script executable
chmod +x app.py

# Install Chrome and dependencies for Render environment
apt-get update && apt-get install -y wget gnupg
wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -
echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list
apt-get update && apt-get install -y google-chrome-stable

# Set display port for Selenium
export DISPLAY=:99

# Start the application using the PORT provided by Render
exec python app.py
