import json
import os
import threading
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scraper import XbetScraper

app = FastAPI(
    title="1xBet Odds API",
    description="API to get live and upcoming 1xBet odds and match information",
    version="1.0.0"
)

# Add CORS middleware to allow cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global data store
data_store = {
    "live_events": [],
    "upcoming_events": [],
    "leagues": [],
    "last_update": None,
    "is_updating": False
}

# Create a lock for thread safety
data_lock = threading.Lock()

# Initialize the scraper
scraper = None

def update_data():
    """Background task to update the data"""
    global scraper, data_store
    
    try:
        if scraper is None:
            scraper = XbetScraper()
        
        while True:
            try:
                with data_lock:
                    data_store["is_updating"] = True
                
                # Get the data from the scraper
                html_content = scraper.get_page_content()
                if html_content:
                    live_events = scraper.parse_live_events(html_content)
                    upcoming_events = scraper.parse_upcoming_events(html_content)
                    
                    # Update leagues every 10 minutes
                    current_time = time.time()
                    if not data_store["last_update"] or current_time - data_store.get("last_leagues_update", 0) > 600:
                        leagues = scraper.get_all_leagues(html_content)
                        with data_lock:
                            data_store["leagues"] = leagues
                            data_store["last_leagues_update"] = current_time
                    
                    # Update the data store with thread safety
                    with data_lock:
                        data_store["live_events"] = live_events
                        data_store["upcoming_events"] = upcoming_events
                        data_store["last_update"] = current_time
                        data_store["is_updating"] = False
                    
                    # Save the data to files for persistence
                    try:
                        os.makedirs("data", exist_ok=True)
                        
                        with open("data/live_events.json", "w", encoding="utf-8") as f:
                            json.dump(live_events, f, ensure_ascii=False, indent=2)
                        
                        with open("data/upcoming_events.json", "w", encoding="utf-8") as f:
                            json.dump(upcoming_events, f, ensure_ascii=False, indent=2)
                        
                        with open("data/leagues.json", "w", encoding="utf-8") as f:
                            json.dump(data_store["leagues"], f, ensure_ascii=False, indent=2)
                            
                        print(f"Data updated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    except Exception as e:
                        print(f"Error saving data to files: {e}")
                
                # Sleep for 20 seconds on Render free tier to avoid excessive resource use
                time.sleep(20)
            except Exception as e:
                print(f"Error in update loop: {e}")
                with data_lock:
                    data_store["is_updating"] = False
                time.sleep(60)  # Wait longer if there was an error
    except Exception as e:
        print(f"Fatal error in update thread: {e}")
        if scraper:
            try:
                scraper.__del__()
            except:
                pass

# Load any existing data from files
try:
    os.makedirs("data", exist_ok=True)
    
    if os.path.exists("data/live_events.json"):
        with open("data/live_events.json", "r", encoding="utf-8") as f:
            data_store["live_events"] = json.load(f)
    
    if os.path.exists("data/upcoming_events.json"):
        with open("data/upcoming_events.json", "r", encoding="utf-8") as f:
            data_store["upcoming_events"] = json.load(f)
    
    if os.path.exists("data/leagues.json"):
        with open("data/leagues.json", "r", encoding="utf-8") as f:
            data_store["leagues"] = json.load(f)
except Exception as e:
    print(f"Error loading data from files: {e}")

@app.on_event("startup")
def startup_event():
    """Start the background update task when the API starts"""
    # Start the update thread
    update_thread = threading.Thread(target=update_data, daemon=True)
    update_thread.start()

@app.get("/")
def read_root():
    """Root endpoint with API info"""
    return {
        "name": "1xBet Odds API",
        "version": "1.0.0",
        "endpoints": [
            "/api/live",
            "/api/upcoming",
            "/api/leagues",
            "/api/live/{league_id}",
            "/api/upcoming/{league_id}",
            "/api/match/{match_id}",
            "/api/status"
        ]
    }

@app.get("/api/status")
def get_status():
    """Get the API status and last update time"""
    with data_lock:
        return {
            "status": "updating" if data_store["is_updating"] else "idle",
            "last_update": datetime.fromtimestamp(data_store["last_update"]).strftime("%Y-%m-%d %H:%M:%S") if data_store["last_update"] else None,
            "live_events_count": len(data_store["live_events"]),
            "upcoming_events_count": len(data_store["upcoming_events"]),
            "leagues_count": len(data_store["leagues"])
        }

@app.get("/api/live")
def get_live_events(
    sport: Optional[str] = Query(None, description="Filter by sport name"),
    league: Optional[str] = Query(None, description="Filter by league name"),
    team: Optional[str] = Query(None, description="Filter by team name")
):
    """Get all live events with optional filters"""
    with data_lock:
        events = data_store["live_events"]
    
    # Apply filters
    if sport:
        events = [e for e in events if e.get("sport", "").lower() == sport.lower()]
    if league:
        events = [e for e in events if e.get("league", "").lower() == league.lower()]
    if team:
        events = [e for e in events if (
            team.lower() in e.get("team1", "").lower() or 
            team.lower() in e.get("team2", "").lower()
        )]
    
    return {"count": len(events), "events": events}

@app.get("/api/upcoming")
def get_upcoming_events(
    sport: Optional[str] = Query(None, description="Filter by sport name"),
    league: Optional[str] = Query(None, description="Filter by league name"),
    team: Optional[str] = Query(None, description="Filter by team name"),
    date: Optional[str] = Query(None, description="Filter by match date")
):
    """Get all upcoming events with optional filters"""
    with data_lock:
        events = data_store["upcoming_events"]
    
    # Apply filters
    if sport:
        events = [e for e in events if e.get("sport", "").lower() == sport.lower()]
    if league:
        events = [e for e in events if e.get("league", "").lower() == league.lower()]
    if team:
        events = [e for e in events if (
            team.lower() in e.get("team1", "").lower() or 
            team.lower() in e.get("team2", "").lower()
        )]
    if date:
        events = [e for e in events if e.get("match_date", "") == date]
    
    return {"count": len(events), "events": events}

@app.get("/api/leagues")
def get_leagues(
    sport: Optional[str] = Query(None, description="Filter by sport name")
):
    """Get all leagues with optional sport filter"""
    with data_lock:
        leagues = data_store["leagues"]
    
    if sport:
        leagues = [l for l in leagues if l.get("sport", "").lower() == sport.lower()]
    
    return {"count": len(leagues), "leagues": leagues}

@app.get("/api/live/{league_id}")
def get_live_events_by_league(league_id: str):
    """Get all live events for a specific league"""
    with data_lock:
        events = [e for e in data_store["live_events"] if e.get("league", "").replace(" ", "_").lower() == league_id.replace("_", " ").lower()]
    
    if not events:
        # Try by exact league name
        with data_lock:
            events = [e for e in data_store["live_events"] if e.get("league", "").lower() == league_id.replace("_", " ").lower()]
    
    return {"count": len(events), "events": events}

@app.get("/api/upcoming/{league_id}")
def get_upcoming_events_by_league(league_id: str):
    """Get all upcoming events for a specific league"""
    with data_lock:
        events = [e for e in data_store["upcoming_events"] if e.get("league", "").replace(" ", "_").lower() == league_id.replace("_", " ").lower()]
    
    if not events:
        # Try by exact league name
        with data_lock:
            events = [e for e in data_store["upcoming_events"] if e.get("league", "").lower() == league_id.replace("_", " ").lower()]
    
    return {"count": len(events), "events": events}

@app.get("/api/match/{match_id}")
def get_match_by_id(match_id: str):
    """Get detailed information for a specific match"""
    match_id = match_id.lower()
    
    with data_lock:
        # Look in live events first
        for event in data_store["live_events"]:
            if event.get("match_id", "").lower() == match_id or event.get("match_id", "").replace(" ", "_").lower() == match_id:
                return event
        
        # Then look in upcoming events
        for event in data_store["upcoming_events"]:
            if event.get("match_id", "").lower() == match_id or event.get("match_id", "").replace(" ", "_").lower() == match_id:
                return event
    
    raise HTTPException(status_code=404, detail="Match not found")

@app.get("/api/refresh", status_code=202)
def trigger_refresh(background_tasks: BackgroundTasks):
    """Manually trigger a data refresh"""
    global scraper
    
    if data_store["is_updating"]:
        return {"status": "already_updating", "message": "Data is already being updated"}
    
    # Restart the scraper if it exists
    if scraper:
        try:
            scraper.__del__()
        except:
            pass
        scraper = None
    
    # Use background task to avoid blocking the request
    background_tasks.add_task(update_data)
    return {"status": "refresh_triggered", "message": "Data refresh has been triggered"}

@app.get("/api/sports")
def get_sports():
    """Get list of available sports"""
    sports = set()
    
    with data_lock:
        # Collect sports from both live and upcoming events
        for event in data_store["live_events"]:
            if "sport" in event:
                sports.add(event["sport"])
        
        for event in data_store["upcoming_events"]:
            if "sport" in event:
                sports.add(event["sport"])
    
    return {"count": len(sports), "sports": sorted(list(sports))}

# Webhook for monitoring services to ping to keep the service alive
@app.get("/ping")
def ping():
    """Endpoint for monitoring services to ping to keep the service alive"""
    return {"status": "alive", "timestamp": datetime.now().isoformat()}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
