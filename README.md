# OpenAI Status Monitor — Web App

A real-time web dashboard that monitors status pages for incidents and outages.

## Run Locally

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

## Deploy Free on Render.com

1. Push this folder to a GitHub repo
2. Go to https://render.com → New → Web Service
3. Connect your GitHub repo
4. Set:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python app.py`
5. Click Deploy → you get a free public URL!

## Deploy Free on Railway.app

```bash
npm install -g @railway/cli
railway login
railway init
railway up
```

## How It Works

- Uses Atom/RSS feeds (lightweight XML, not full page scraping)
- HTTP Content-Hash fallback for change detection
- asyncio for concurrent monitoring of multiple feeds
- WebSocket for real-time browser updates (no page refresh needed)
