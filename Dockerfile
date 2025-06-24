FROM python:3.11-slim

# Install dependencies
RUN apt-get update && apt-get install -y \
    chromium chromium-driver curl unzip gnupg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables for Chrome
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Install Python dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Start the app
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--timeout", "120"]