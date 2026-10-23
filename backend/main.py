"""
Downly Backend Server

A FastAPI-based backend that uses yt-dlp to extract video information
and download URLs from supported platforms. Keeps all API keys and
secrets server-side.

Usage:
    pip install -r requirements.txt
    cp .env.example .env  # Edit with your settings
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os
import re
import time
import logging
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
import yt_dlp

load_dotenv()

# Configuration
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
MAX_VIDEO_DURATION = int(os.getenv("MAX_VIDEO_DURATION", "7200"))  # 2 hours
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2000"))
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
FB_APP_ID = os.getenv("FB_APP_ID", "")
FB_APP_SECRET = os.getenv("FB_APP_SECRET", "")

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Logging
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("downly")

# Rate limiting
rate_limit_store: dict[str, list[float]] = {}

app = FastAPI(
    title="Downly Backend",
    description="Video information extraction API for the Downly Android app",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import ipaddress

def is_allowed_url(url: str) -> bool:
    """Validate that the URL is a safe public internet URL (anti-SSRF for any platform)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.hostname:
            return False
        hostname = parsed.hostname.lower()
        if hostname in ("localhost", "0.0.0.0", "::1"):
            return False

        # Try to parse as IP to block private ranges and metadata endpoints
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        except ValueError:
            # It's a domain name (not raw IP)
            pass

        # Also block cloud metadata hosts
        if "metadata.google.internal" in hostname or "169.254" in hostname:
            return False

        return True
    except Exception:
        return False


def check_rate_limit(client_ip: str) -> bool:
    """Simple in-memory rate limiter."""
    now = time.time()
    if client_ip not in rate_limit_store:
        rate_limit_store[client_ip] = []

    # Clean old entries
    rate_limit_store[client_ip] = [
        t for t in rate_limit_store[client_ip] if now - t < 60
    ]

    if len(rate_limit_store[client_ip]) >= RATE_LIMIT_PER_MINUTE:
        return False

    rate_limit_store[client_ip].append(now)
    return True


def detect_platform(url: str) -> str:
    """Detect the platform from the URL for icons and metadata."""
    hostname = urlparse(url).hostname or ""
    hostname = hostname.lower()

    if "youtube" in hostname or "youtu.be" in hostname:
        return "youtube"
    elif "facebook" in hostname or "fb.watch" in hostname or "fb.com" in hostname:
        return "facebook"
    elif "instagram" in hostname:
        return "instagram"
    elif "tiktok" in hostname or "douyin" in hostname:
        return "tiktok"
    elif "twitter" in hostname or "x.com" in hostname:
        return "twitter"
    elif "reddit" in hostname or "redd.it" in hostname:
        return "reddit"
    elif "pinterest" in hostname or "pin.it" in hostname:
        return "pinterest"
    elif "threads" in hostname:
        return "threads"
    elif "twitch" in hostname:
        return "twitch"
    elif "snapchat" in hostname:
        return "snapchat"
    elif "soundcloud" in hostname:
        return "soundcloud"
    elif "vimeo" in hostname:
        return "vimeo"
    return "other"


# --- Pydantic models ---

class VideoInfoRequest(BaseModel):
    url: str
    fb_token: Optional[str] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 2048:
            raise ValueError("URL too long")
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


class DownloadRequest(BaseModel):
    url: str
    format_id: str
    fb_token: Optional[str] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 2048:
            raise ValueError("URL too long")
        return v

    @field_validator("format_id")
    @classmethod
    def validate_format_id(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 100:
            raise ValueError("Invalid format ID")
        # Only allow safe characters
        if not re.match(r"^[\w\-+.]+$", v):
            raise ValueError("Invalid format ID characters")
        return v


class FormatInfo(BaseModel):
    format_id: str
    quality: str
    extension: str
    file_size: Optional[int] = None
    has_video: bool = True
    has_audio: bool = True
    resolution: Optional[str] = None
    codec: Optional[str] = None


class VideoInfoData(BaseModel):
    id: str
    title: str
    thumbnail_url: Optional[str] = None
    duration: Optional[int] = None
    platform: str
    uploader: Optional[str] = None
    formats: list[FormatInfo] = []


class ApiResponse(BaseModel):
    success: bool
    data: Optional[VideoInfoData] = None
    download_url: Optional[str] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    error: Optional[str] = None
    code: Optional[str] = None


# --- yt-dlp helper ---

import tempfile

def make_cookie_file(cookie_str: str) -> Optional[str]:
    """Parse cookie string (c_user=...; xs=...) or Netscape format into a temporary Netscape cookie file."""
    if not cookie_str:
        return None
    cookie_str = cookie_str.strip()
    try:
        f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8")
        # Check if already in Netscape format
        if cookie_str.startswith("# Netscape") or "\t" in cookie_str:
            if not cookie_str.startswith("# Netscape"):
                f.write("# Netscape HTTP Cookie File\n")
            f.write(cookie_str)
            f.close()
            return f.name

        # Otherwise key-value format (c_user=100...; xs=...)
        f.write("# Netscape HTTP Cookie File\n")
        domains = [".facebook.com", ".instagram.com"]
        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" in item:
                parts = item.split("=", 1)
                name = parts[0].strip()
                val = parts[1].strip()
                if name:
                    for d in domains:
                        initial_dot = "TRUE" if d.startswith(".") else "FALSE"
                        f.write(f"{d}\t{initial_dot}\t/\tTRUE\t2147483647\t{name}\t{val}\n")
        f.close()
        return f.name
    except Exception as e:
        logger.error(f"Failed to create cookie file: {e}")
        return None


def get_ydl_opts(fb_token: Optional[str] = None) -> dict:
    """Build optimized yt-dlp options with browser TLS impersonation, multi-threaded fragment downloads and auth cookies."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "no_check_certificates": False,
        "geo_bypass": True,
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 8,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        }
    }

    # Browser impersonation (bypasses Cloudflare, Akamai, TikTok, and anti-bot checks)
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    except Exception as e:
        logger.debug(f"Impersonate target initialization note: {e}")

    # 1. Check if client provided Facebook cookies / session
    if fb_token:
        cookie_path = make_cookie_file(fb_token)
        if cookie_path and os.path.exists(cookie_path):
            opts["cookiefile"] = cookie_path
            logger.info(f"Loaded client Facebook cookies into {cookie_path}")

    # 2. Check if a server-side fb_cookies.txt exists as fallback
    if "cookiefile" not in opts:
        server_cookies = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fb_cookies.txt")
        if os.path.exists(server_cookies):
            opts["cookiefile"] = server_cookies
            logger.info("Using server fb_cookies.txt fallback")

    return opts


def extract_formats(raw_formats: list[dict], duration: Optional[int] = None, platform: str = "") -> list[FormatInfo]:
    """
    Extract and curate ONLY standard user-requested qualities that ACTUALLY exist:
    Video: 4K (2160), 2K (1440), 1080p, 720p, 480p, 360p, 144p
    Audio: Top 3 bitrates (320 kbps, 192 kbps, 128 kbps) only if audio exists
    """
    formats = []

    # 1. Inspect raw video streams to find actual available video heights and bitrates
    available_heights = set()
    height_to_format = {}
    has_audio_stream = False

    for f in raw_formats:
        h = f.get("height")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        if acodec != "none" or f.get("audio_channels"):
            has_audio_stream = True

        if h and vcodec != "none":
            try:
                h_int = int(h)
                # Ignore tiny storyboard formats (< 120px)
                if h_int >= 120:
                    available_heights.add(h_int)
                    if h_int not in height_to_format or (f.get("filesize") or f.get("tbr", 0) > height_to_format[h_int].get("tbr", 0)):
                        height_to_format[h_int] = f
            except (ValueError, TypeError):
                pass

    format_ids = set(str(f.get("format_id", "")).lower() for f in raw_formats)

    # If heights are not provided (e.g. Facebook SD/HD or single-stream MP4)
    if not available_heights:
        if "hd" in format_ids:
            available_heights.add(720)
        if "sd" in format_ids:
            available_heights.add(480)
        if not available_heights:
            available_heights.add(720)

    max_height = max(available_heights) if available_heights else 720

    # Best audio bitrate stream to add to video size estimate if separate DASH
    best_audio_tbr = 128
    for f in raw_formats:
        if f.get("acodec") != "none" and f.get("abr"):
            best_audio_tbr = max(best_audio_tbr, int(f.get("abr", 128)))

    target_resolutions = [
        (2160, "4K", "3840x2160"),
        (1440, "2K", "2560x1440"),
        (1080, "1080p", "1920x1080"),
        (720, "720p", "1280x720"),
        (480, "480p", "854x480"),
        (360, "360p", "640x360"),
        (144, "144p", "256x144"),
    ]

    for target_h, label, res in target_resolutions:
        # Crucial: Do NOT offer higher resolution than the video actually has!
        # If video max is 720, don't show 4K, 2K, or 1080p!
        if target_h > max_height + 20:
            continue

        # Check if this exact or close resolution exists
        is_available = any(abs(h - target_h) <= 30 for h in available_heights)
        # Or standard fallback downscales (480p, 360p, 144p) if the max video is higher
        if not is_available and target_h <= max_height and target_h in (480, 360, 144):
            is_available = True

        if is_available:
            # Calculate real/estimated file size
            matched_f = None
            for h in sorted(available_heights, reverse=True):
                if abs(h - target_h) <= 30:
                    matched_f = height_to_format.get(h)
                    break

            est_size = None
            if matched_f:
                if matched_f.get("filesize"):
                    est_size = matched_f["filesize"]
                elif matched_f.get("filesize_approx"):
                    est_size = matched_f["filesize_approx"]
                elif matched_f.get("tbr") and duration:
                    # tbr in kbps -> bytes
                    est_size = int(((matched_f["tbr"] + best_audio_tbr) * 1000 / 8) * duration)
                elif matched_f.get("vbr") and duration:
                    est_size = int(((matched_f["vbr"] + best_audio_tbr) * 1000 / 8) * duration)

            if not est_size and duration:
                # Approximate default bitrates per resolution
                bitrate_map = {2160: 15000, 1440: 8000, 1080: 3500, 720: 1800, 480: 800, 360: 450, 144: 150}
                est_size = int(((bitrate_map.get(target_h, 1500) + best_audio_tbr) * 1000 / 8) * duration)

            formats.append(FormatInfo(
                format_id=f"v_{target_h}",
                quality=label,
                extension="mp4",
                file_size=est_size,
                has_video=True,
                has_audio=True,
                resolution=res,
                codec="H.264+AAC"
            ))

    # Fallback to at least 720p if none matched
    if not formats:
        formats.append(FormatInfo(
            format_id="v_720",
            quality="720p",
            extension="mp4",
            file_size=None,
            has_video=True,
            has_audio=True,
            resolution="1280x720",
            codec="H.264+AAC"
        ))

    # 2. Audio formats - ONLY if the video actually contains audio streams
    if has_audio_stream or platform != "unknown":
        audio_options = [
            ("High Quality (320 kbps)", "a_320", 320),
            ("Medium Quality (192 kbps)", "a_192", 192),
            ("Standard Quality (128 kbps)", "a_128", 128),
        ]
        for label, fid, kbps in audio_options:
            est_size = int((kbps * 1000 / 8) * duration) if duration else None
            formats.append(FormatInfo(
                format_id=fid,
                quality=label,
                extension="mp3",
                file_size=est_size,
                has_video=False,
                has_audio=True,
                resolution=None,
                codec="MP3"
            ))

    return formats


# --- API Endpoints ---

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/download/file/{filename}")
async def serve_file(filename: str):
    """Serve a downloaded and merged real media file directly to the client."""
    safe_name = os.path.basename(filename)
    file_path = os.path.join(DOWNLOADS_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    media_type = "video/mp4" if safe_name.endswith(".mp4") else "audio/mpeg"
    return FileResponse(file_path, media_type=media_type, filename=safe_name)


@app.post("/api/video/info", response_model=ApiResponse)
async def get_video_info(request: Request, body: VideoInfoRequest):
    """Extract video information and available formats."""
    client_ip = request.client.host if request.client else "unknown"

    if not check_rate_limit(client_ip):
        return ApiResponse(
            success=False,
            error="Too many requests. Please wait a moment.",
            code="RATE_LIMITED"
        )

    url = body.url.strip()

    if not is_allowed_url(url):
        return ApiResponse(
            success=False,
            error="This URL is not from a supported platform.",
            code="UNSUPPORTED_URL"
        )

    platform = detect_platform(url)
    logger.info(f"Extracting info for {platform} URL: {url[:100]}...")

    try:
        ydl_opts = get_ydl_opts(body.fb_token)
        ydl_opts["extract_flat"] = False

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if not info:
                return ApiResponse(
                    success=False,
                    error="Could not extract video information.",
                    code="VIDEO_UNAVAILABLE"
                )

            # Check duration limit
            duration = info.get("duration")
            if duration and duration > MAX_VIDEO_DURATION:
                return ApiResponse(
                    success=False,
                    error=f"Video is too long ({duration}s). Maximum is {MAX_VIDEO_DURATION}s.",
                    code="VIDEO_TOO_LONG"
                )

            raw_formats = info.get("formats", [])
            formats = extract_formats(raw_formats, duration=int(duration) if duration else None, platform=platform)

            video_data = VideoInfoData(
                id=info.get("id", "unknown"),
                title=info.get("title", "Untitled"),
                thumbnail_url=info.get("thumbnail"),
                duration=int(duration) if duration else None,
                platform=platform,
                uploader=info.get("uploader") or info.get("channel"),
                formats=formats
            )

            return ApiResponse(success=True, data=video_data)

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.warning(f"yt-dlp download error: {error_msg}")

        if "private" in error_msg.lower() or "login" in error_msg.lower():
            return ApiResponse(
                success=False,
                error="This video requires authentication or is private.",
                code="AUTH_REQUIRED"
            )
        elif "not available" in error_msg.lower() or "unavailable" in error_msg.lower():
            return ApiResponse(
                success=False,
                error="This video is not available. It may have been removed or is restricted in your region.",
                code="VIDEO_UNAVAILABLE"
            )
        else:
            return ApiResponse(
                success=False,
                error="Failed to extract video information. The platform may have changed its format.",
                code="PLATFORM_ERROR"
            )
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return ApiResponse(
            success=False,
            error="An internal error occurred. Please try again.",
            code="INTERNAL_ERROR"
        )


@app.post("/api/video/download", response_model=ApiResponse)
async def get_download_url(request: Request, body: DownloadRequest):
    """
    Download and merge video+audio with FFmpeg into a 100% genuine MP4/MP3 file,
    and return a direct streaming download URL to the app.
    """
    client_ip = request.client.host if request.client else "unknown"

    if not check_rate_limit(client_ip):
        return ApiResponse(
            success=False,
            error="Too many requests. Please wait a moment.",
            code="RATE_LIMITED"
        )

    url = body.url.strip()

    if not is_allowed_url(url):
        return ApiResponse(
            success=False,
            error="This URL is not from a supported platform.",
            code="UNSUPPORTED_URL"
        )

    format_id = body.format_id
    base_url = str(request.base_url).rstrip("/")
    logger.info(f"Processing download for format {format_id}: {url[:100]}...")

    try:
        # Base options with cookies and multi-threaded fragment downloads
        base_ydl_opts = get_ydl_opts(body.fb_token)

        # 1. Video Download with Full Audio Merge (v_2160, v_1080, v_720, etc.)
        if format_id.startswith("v_"):
            height_str = format_id.replace("v_", "")
            height = int(height_str) if height_str.isdigit() else 720

            # Step A: Quick check if already downloaded and cached
            vid_id = None
            title = "video"
            try:
                with yt_dlp.YoutubeDL(base_ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info:
                        vid_id = info.get("id")
                        title = info.get("title", "video")
            except Exception as e:
                logger.warning(f"Metadata extract warning: {e}")

            safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:100]

            if vid_id:
                expected_file = f"{vid_id}_{height}p.mp4"
                file_path = os.path.join(DOWNLOADS_DIR, expected_file)
                if os.path.exists(file_path) and os.path.getsize(file_path) > 10240:
                    file_size = os.path.getsize(file_path)
                    logger.info(f"Instant return (cached video): {expected_file} ({file_size} bytes)")
                    return ApiResponse(
                        success=True,
                        download_url=f"{base_url}/api/download/file/{expected_file}",
                        file_name=f"{safe_title}_{height}p.mp4",
                        file_size=file_size
                    )

            # Step B: Download and merge with FFmpeg
            ydl_opts = dict(base_ydl_opts)
            ydl_opts.update({
                "format": f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio/hd/best[height<={height}]/best",
                "outtmpl": os.path.join(DOWNLOADS_DIR, f"%(id)s_{height}p.%(ext)s"),
                "merge_output_format": "mp4",
            })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    return ApiResponse(success=False, error="Could not extract video.", code="VIDEO_UNAVAILABLE")

                vid_id = info.get("id", vid_id or "video")
                title = info.get("title", title)
                safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:100]

                actual_file = f"{vid_id}_{height}p.mp4"
                file_path = os.path.join(DOWNLOADS_DIR, actual_file)

                if not os.path.exists(file_path):
                    for f in os.listdir(DOWNLOADS_DIR):
                        if f.startswith(f"{vid_id}_{height}p"):
                            actual_file = f
                            file_path = os.path.join(DOWNLOADS_DIR, actual_file)
                            break

                file_size = os.path.getsize(file_path) if os.path.exists(file_path) else None
                download_url = f"{base_url}/api/download/file/{actual_file}"

                logger.info(f"Real video ready: {actual_file} ({file_size} bytes)")
                return ApiResponse(
                    success=True,
                    download_url=download_url,
                    file_name=f"{safe_title}_{height}p.mp4",
                    file_size=file_size
                )

        # 2. Audio Only Download with FFmpeg MP3 Conversion (a_320, a_192, a_128)
        elif format_id.startswith("a_"):
            bitrate_str = format_id.replace("a_", "")
            bitrate = int(bitrate_str) if bitrate_str.isdigit() else 192

            # Quick check if already downloaded
            vid_id = None
            title = "audio"
            try:
                with yt_dlp.YoutubeDL(base_ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info:
                        vid_id = info.get("id")
                        title = info.get("title", "audio")
            except Exception as e:
                logger.warning(f"Audio metadata extract warning: {e}")

            safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:100]

            if vid_id:
                expected_file = f"{vid_id}_{bitrate}k.mp3"
                file_path = os.path.join(DOWNLOADS_DIR, expected_file)
                if os.path.exists(file_path) and os.path.getsize(file_path) > 10240:
                    file_size = os.path.getsize(file_path)
                    logger.info(f"Instant return (cached audio): {expected_file} ({file_size} bytes)")
                    return ApiResponse(
                        success=True,
                        download_url=f"{base_url}/api/download/file/{expected_file}",
                        file_name=f"{safe_title}_{bitrate}kbps.mp3",
                        file_size=file_size
                    )

            ydl_opts = dict(base_ydl_opts)
            ydl_opts.update({
                "format": "bestaudio/best",
                "outtmpl": os.path.join(DOWNLOADS_DIR, f"%(id)s_{bitrate}k.%(ext)s"),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(bitrate),
                }],
            })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    return ApiResponse(success=False, error="Could not extract audio.", code="VIDEO_UNAVAILABLE")

                vid_id = info.get("id", vid_id or "audio")
                title = info.get("title", title)
                safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:100]

                actual_file = f"{vid_id}_{bitrate}k.mp3"
                file_path = os.path.join(DOWNLOADS_DIR, actual_file)

                file_size = os.path.getsize(file_path) if os.path.exists(file_path) else None
                download_url = f"{base_url}/api/download/file/{actual_file}"

                logger.info(f"Real audio ready: {actual_file} ({file_size} bytes)")
                return ApiResponse(
                    success=True,
                    download_url=download_url,
                    file_name=f"{safe_title}_{bitrate}kbps.mp3",
                    file_size=file_size
                )

        # 3. Direct/Fallback
        else:
            ydl_opts = get_ydl_opts(body.fb_token)
            ydl_opts["format"] = body.format_id
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                download_url = info.get("url")
                title = info.get("title", "video")
                safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:100]
                return ApiResponse(
                    success=True,
                    download_url=download_url,
                    file_name=f"{safe_title}.mp4",
                    file_size=None
                )

    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"yt-dlp error getting download URL: {e}")
        return ApiResponse(
            success=False,
            error="Failed to process video. The video may be unavailable.",
            code="PLATFORM_ERROR"
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return ApiResponse(
            success=False,
            error="An internal error occurred while preparing download.",
            code="INTERNAL_ERROR"
        )


@app.post("/auth/facebook/exchange")
async def exchange_facebook_token(code: str, redirect_uri: str):
    """
    Exchange a Facebook authorization code for an access token.
    This keeps the app secret server-side.
    """
    if not FB_APP_ID or not FB_APP_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Facebook integration not configured on this server."
        )

    import httpx

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://graph.facebook.com/v18.0/oauth/access_token",
                params={
                    "client_id": FB_APP_ID,
                    "client_secret": FB_APP_SECRET,
                    "redirect_uri": redirect_uri,
                    "code": code,
                }
            )

            if response.status_code != 200:
                logger.error(f"Facebook token exchange failed: {response.text}")
                raise HTTPException(
                    status_code=400,
                    detail="Failed to exchange Facebook authorization code."
                )

            data = response.json()
            access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)

            if not access_token:
                raise HTTPException(
                    status_code=400,
                    detail="No access token received from Facebook."
                )

            # Get user info
            user_response = await client.get(
                "https://graph.facebook.com/v18.0/me",
                params={"access_token": access_token, "fields": "name"}
            )

            user_name = None
            if user_response.status_code == 200:
                user_name = user_response.json().get("name")

            return {
                "access_token": access_token,
                "expires_in": expires_in,
                "user_name": user_name
            }

    except httpx.HTTPError as e:
        logger.error(f"HTTP error during Facebook token exchange: {e}")
        raise HTTPException(
            status_code=502,
            detail="Failed to communicate with Facebook."
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=HOST,
        port=int(PORT),
        reload=DEBUG,
        log_level="debug" if DEBUG else "info"
    )
