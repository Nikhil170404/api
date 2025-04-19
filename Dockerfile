FROM python:3.9-slim

WORKDIR /app

# Install system dependencies with better error handling
RUN apt-get update && apt-get install -y \
    wget \
    gnupg2 \
    apt-utils \
    unzip \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome with more robust approach
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor > /usr/share/keyrings/google-chrome-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" | tee /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && echo "Chrome version: $(google-chrome --version)" \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Make start script executable
RUN chmod +x start.sh

# Create data directory
RUN mkdir -p data

# Set environment variable for Chrome to run in container
ENV PYTHONUNBUFFERED=1

# Command to run the application
CMD ["/bin/bash", "start.sh"]
