# Downly Backend Dockerfile
FROM python:3.12-slim

# Install system dependencies including FFmpeg (required for yt-dlp merging)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency requirements
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source code
COPY . .

# Create downloads directory
RUN mkdir -p downloads

# Set default port
ENV PORT=8000
EXPOSE 8000

# Start server using the PORT environment variable provided by cloud hosts
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
