# Downly Backend Server

A FastAPI-based backend server for the Downly Android app. Uses yt-dlp to extract video information and download URLs from supported platforms.

## Features

- Video info extraction from YouTube, Facebook, Instagram, TikTok, X/Twitter
- Format/quality listing with file size estimates
- Direct download URL generation
- Facebook OAuth token exchange (keeps app secret server-side)
- Rate limiting and SSRF protection
- URL validation against allowed platforms

## Requirements

- Python 3.10+
- pip

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

3. **Run the server:**
   ```bash
   python main.py
   # Or:
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

## API Endpoints

### `GET /health`
Health check. Returns `{"status": "ok", "version": "1.0.0"}`.

### `POST /api/video/info`
Extract video metadata and available formats.

**Request:**
```json
{
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "fb_token": null
}
```

**Response:**
```json
{
    "success": true,
    "data": {
        "id": "dQw4w9WgXcQ",
        "title": "Video Title",
        "thumbnail_url": "https://...",
        "duration": 212,
        "platform": "youtube",
        "uploader": "Channel Name",
        "formats": [
            {
                "format_id": "137+140",
                "quality": "1080p",
                "extension": "mp4",
                "file_size": 50000000,
                "has_video": true,
                "has_audio": true,
                "resolution": "1920x1080",
                "codec": "avc1+mp4a"
            }
        ]
    }
}
```

### `POST /api/video/download`
Get a direct download URL for a specific format.

**Request:**
```json
{
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "format_id": "137+140"
}
```

### `POST /auth/facebook/exchange`
Exchange a Facebook authorization code for an access token.

## Security

- All URLs are validated against allowed platform hosts (anti-SSRF)
- Private/internal IPs are blocked
- Rate limiting (configurable per minute)
- Facebook app secret is never exposed to the client
- Format IDs are validated for safe characters

## Deployment

For production, use a reverse proxy (nginx) with HTTPS:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
```

## Limitations

- Some videos require platform-specific authentication
- Private/restricted videos may not be accessible
- Download URLs are temporary and expire
- Facebook group videos require approved API permissions from Meta
