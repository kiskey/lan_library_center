FROM python:3.10-slim

# Install system dependencies for PyNaCl (Encryption)
RUN apt-get update && apt-get install -y \
    libsodium-dev \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY templates/ ./templates/

# Expose the dashboard port
EXPOSE 5000

# Set environment to production
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py"]
