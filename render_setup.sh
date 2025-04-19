#!/bin/bash
set -e

# Install Chrome and dependencies
echo "Installing Chrome dependencies..."
apt-get update
apt-get install -y wget gnupg2 apt-utils libxss1 libappindicator1 libindicator7 fonts-liberation xvfb

# Download and install Chrome
echo "Downloading Google Chrome..."
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get install -y ./google-chrome-stable_current_amd64.deb
rm google-chrome-stable_current_amd64.deb

# Print Chrome version
echo "Google Chrome version:"
google-chrome --version

# Download and install ChromeDriver
echo "Installing ChromeDriver..."
CHROME_VERSION=$(google-chrome --version | awk '{print $3}' | cut -d'.' -f1)
CHROMEDRIVER_VERSION=$(curl -s "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_$CHROME_VERSION")
curl -Lo /tmp/chromedriver_linux64.zip "https://chromedriver.storage.googleapis.com/$CHROMEDRIVER_VERSION/chromedriver_linux64.zip"
unzip -q /tmp/chromedriver_linux64.zip -d /tmp
mv /tmp/chromedriver /usr/bin/chromedriver
chmod +x /usr/bin/chromedriver
rm /tmp/chromedriver_linux64.zip

# Print ChromeDriver version
echo "ChromeDriver version:"
chromedriver --version

# Set environment variable for app
echo "CHROMEDRIVER_PATH=/usr/bin/chromedriver" >> $RENDER_ENV_FILE
echo "RENDER=true" >> $RENDER_ENV_FILE

echo "Setup complete!"
