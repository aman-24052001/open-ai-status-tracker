# OpenAI Status Tracker

Automatically monitors the OpenAI status page for incidents, outages, and degradations.

## Features
- Dual polling (RSS feed + JSON API)
- Content-based change detection (no noise)
- Efficient conditional HTTP caching (ETag/304 responses)
- Scales to 100+ providers
- Console output only (no UI or database needed)

## Setup

### Requirements
- Python 3.10+
- httpx, feedparser, pydantic

### Installation
```bash
pip install -r requirements.txt
python openai_status_main.py
```

### Configuration
```bash
# Custom poll interval (default: 30 seconds)
POLL_INTERVAL=15 python openai_status_main.py

# Custom HTTP timeout (default: 15 seconds)
HTTP_TIMEOUT=10 python openai_status_main.py
```

## Example Output
```
[2026-02-24 14:32:00] Product: OpenAI API - Chat Completions
Status: Degraded performance — Investigating upstream issue
```

## Architecture
- **Event-driven**: asyncio.Queue as central event bus
- **Producers**: RSS watcher + JSON watcher (2 per provider)
- **Consumer**: Single task printing to stdout
- **Change Detection**: Content hashing to avoid false alerts
- **Resilience**: Auto-restarting supervised tasks

See `Approach.txt` for detailed design decisions and scalability analysis.

## License
MIT
