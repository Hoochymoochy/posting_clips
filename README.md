# Clip Poster Server Backend

Server backend and remote posting service for DJ set vertical shorts (YouTube Shorts, Instagram Reels, TikTok).

## Architecture

This project runs as a server that:
1. **Receives video clips via HTTP API**:
   Stores uploaded videos and metadata locally under:
   ```text
   clips/
   └── [id]/
       ├── clip.mp4         # Rendered vertical video
       └── metadata.json    # Metadata containing caption, title, tags, etc.
   ```
2. **Polls Supabase in the background**:
   A background worker continuously checks Supabase for due clips (`posted = false` and `scheduled_at <= now()`).
   When a clip is due:
   - Reads `clips/[id]/clip.mp4`
   - Reads `clips/[id]/metadata.json` to extract caption and title
   - Posts to requested social platforms (YouTube, Instagram, TikTok)
   - Updates `channels` and `clips` tables in Supabase with upload status and live URLs.

---

## Setup

```bash
# Clone or navigate to the directory
cd posting_clips

# Create and activate virtual environment
python -m venv venv
# Windows:
.\venv\Scripts\Activate.ps1
# Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Configuration
cp .env.example .env      # Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
cp config.example.json config.json
# All client credentials and platform settings are stored in clients.json
```

---

## Running on a Server

### 1. Start Server with Embedded Background Worker (Recommended)

Starts the FastAPI server on `http://0.0.0.0:8000` with the background polling worker active:

```bash
python server.py
# or using uvicorn directly:
uvicorn server:app --host 0.0.0.0 --port 8000
```

### 2. Run Worker Standalone

If you wish to run the background worker separately from the API server:

```bash
# Continuous queue polling
python worker.py

# Single cycle test without live uploads
python worker.py --once --dry-run

# Custom polling interval (e.g. 15 seconds)
python worker.py --poll-seconds 15
```

---

## API Endpoints

### 1. Upload Clip & Metadata
- **Endpoint**: `POST /api/clips/{id}` (or `POST /api/clips`)
- **Content-Type**: `multipart/form-data`
- **Fields**:
  - `video` (file, required): The MP4 video file.
  - `caption` (text, optional): Caption for Instagram / TikTok / YouTube description.
  - `title` (text, optional): Title for YouTube short.
  - `tags` (text, optional): Comma-separated hashtags (e.g. `DJ,EDM,BoilerRoom`).
  - `metadata_file` (file, optional): Direct `.json` file containing metadata.
  - `save_to_supabase` (bool, optional): If `true`, also registers the clip in Supabase.

#### Example `curl` Upload:
```bash
curl -X POST "http://localhost:8000/api/clips/8ebd49bb-0b87-4dc8-8436-367d52cbd696" \
  -F "video=@my_clip.mp4" \
  -F "caption=Amazing Fred Again transition! 🔥 #DJ #EDM" \
  -F "title=Fred Again Boiler Room"
```

### 2. Inspect Clip Status
- **Endpoint**: `GET /api/clips/{id}`
- Returns local file presence (`clip.mp4`), size in bytes, and parsed `metadata.json`.

### 3. List Stored Clips
- **Endpoint**: `GET /api/clips`
- Lists all clips currently received and stored in `clips/`.

### 4. Health Check
- **Endpoint**: `GET /health`
- Returns Supabase connectivity, background worker status, clips storage count, and social connection flags.

### 5. Social Connections (credentials on this host)
Permissions for auto-posting are stored on the poster machine:

| Platform | Storage |
|---|---|
| YouTube | `token.json` (OAuth; needs `client_secrets.json`) |
| Instagram | `instagram_browser/` Playwright profile |
| TikTok | `tiktok_session/` Playwright profile |
| YouTube | `clients.json` (under `youtube.token` & `youtube.client_secrets`, or fallback `token.json` / `client_secrets.json`) |
| Instagram | `clients.json` (`authorization_data`) & `instagram_browser/` Playwright profile |
| TikTok | `tiktok_session/` Playwright profile (configured in `clients.json`) |

- **Status**: `GET /api/connections`
- **Connect**: `POST /api/connections/{youtube\|instagram\|tiktok}/connect`  
  Opens a browser **on this server** for login. Poll status until `connected: true`.
- **Disconnect**: `POST /api/connections/{platform}/disconnect`

CLI equivalents:
```bash
python uploader.py --setup-youtube
python uploader.py --setup-instagram
python uploader.py --setup-tiktok
```

### 6. Trigger Immediate Poll Cycle
- **Endpoint**: `POST /api/worker/poll-now`
- Forces an immediate polling check against Supabase.

---

## Environment Variables (.env)

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_URL` | - | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | - | Service role secret key |
| `ENABLE_BACKGROUND_WORKER` | `true` | Set to `false` to disable polling on API server |
| `POLL_SECONDS` | `30` | Seconds between queue poll checks |
| `HOST` | `0.0.0.0` | API bind address |
| `PORT` | `8000` | API bind port |
| `DRY_RUN` | `false` | If `true`, validates without live social uploads |
| `YOUTUBE_PRIVACY` | `public` | Default YouTube privacy (`public`, `unlisted`, `private`) |
