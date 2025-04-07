#!/usr/bin/env python3
"""
Cricket Odds API for BetBhai.io - Optimized for Free Render with high concurrency
"""

import os
import re
import time
import json
import logging
import threading
import asyncio
import uvicorn
from typing import List, Dict, Any, Optional, Set
from datetime import datetime
from fastapi import FastAPI, HTTPException, status, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
import aiohttp
from bs4 import BeautifulSoup
from starlette.concurrency import run_in_threadpool
from cachetools import TTLCache, LRUCache
import functools
from collections import deque

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("cricket_odds_api.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Environment variables with defaults
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", 1))  # 1 second
MAX_CLIENTS_PER_MINUTE = int(os.environ.get("MAX_CLIENTS_PER_MINUTE", 1000))
DATA_DIR = os.environ.get('DATA_DIR', 'data')
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "cricket_odds_latest.json")

# Initialize FastAPI app
app = FastAPI(
    title="Cricket Odds API",
    description="Optimized API for real-time cricket odds from betbhai.io",
    version="3.0.0",
)

# Add middleware for performance
app.add_middleware(GZipMiddleware, minimum_size=500)  # Compress responses
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data models
class OddItem(BaseModel):
    position: int
    price: str
    volume: Optional[str] = None

class OddsData(BaseModel):
    back: List[OddItem] = []
    lay: List[OddItem] = []

class Match(BaseModel):
    id: str
    timestamp: str
    team1: Optional[str] = None
    team2: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    in_play: Optional[bool] = False
    score: Optional[List[str]] = None
    odds: Optional[OddsData] = None

class ScraperStatus(BaseModel):
    status: str
    last_updated: Optional[str] = None
    matches_count: int = 0
    is_running: bool
    error_count: int
    uptime_seconds: int = 0
    changes_since_last_update: int = 0

# Memory-efficient caching system
# Cache for API responses (10 second TTL, max 100 items)
response_cache = TTLCache(maxsize=100, ttl=10)

# Cache for rate limiting (1 minute TTL, max 10000 clients)
rate_limit_cache = TTLCache(maxsize=10000, ttl=60)

# Global state with lock for thread safety
scraper_state = {
    "data": {"matches": []},
    "status": "idle",
    "last_updated": None,
    "is_running": False,
    "start_time": None,
    "error_count": 0,
    "changes_since_last_update": 0,
    "lock": threading.Lock()
}

# Rate limiting middleware
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Get client IP
    client_ip = request.client.host if request.client else "unknown"
    
    # Check rate limit
    current_time = time.time()
    minute_bucket = int(current_time / 60)
    rate_key = f"{client_ip}:{minute_bucket}"
    
    # Get or create counter for this client
    with scraper_state["lock"]:
        count = rate_limit_cache.get(rate_key, 0)
        
        if count >= MAX_CLIENTS_PER_MINUTE:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded. Please try again later."}
            )
        
        # Increment counter
        rate_limit_cache[rate_key] = count + 1
    
    # Process the request
    response = await call_next(request)
    return response

# Cache decorator for API endpoints
def cached(ttl_seconds=10):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Create a cache key from the function name and arguments
            key = f"{func.__name__}:{str(args)}:{str(kwargs)}"
            
            # Check if result is in cache
            cached_result = response_cache.get(key)
            if cached_result is not None:
                return cached_result
            
            # Execute function
            result = await func(*args, **kwargs)
            
            # Cache the result
            response_cache[key] = result
            
            return result
        return wrapper
    return decorator

# Optimized cricket odds scraper using aiohttp and BeautifulSoup
class CricketOddsScraper:
    def __init__(self, url="https://www.betbhai.io/"):
        self.url = url
        self.error_count = 0
        self.max_continuous_errors = 5
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        # Error backoff mechanism
        self.backoff_time = 1
        self.max_backoff = 30
        # Request optimization
        self.session = None
        self.last_successful_fetch = None
        # Memory management - store last 10 pages to detect changes
        self.page_history = deque(maxlen=3)
    
    async def setup_session(self):
        """Set up aiohttp session with connection pooling"""
        if self.session is None or self.session.closed:
            conn = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            timeout = aiohttp.ClientTimeout(total=15)
            self.session = aiohttp.ClientSession(connector=conn, timeout=timeout)
        return self.session
    
    def _create_stable_id(self, team1: str, team2: str) -> str:
        """Create a stable ID based on team names"""
        if not team1:
            return "unknown_match"
        
        # Sort team names for consistency
        teams = sorted([team1, team2]) if team2 and team1 != team2 else [team1]
        
        # Normalize team names
        normalized = []
        for team in teams:
            team = "".join(c.lower() if c.isalnum() else '_' for c in team)
            team = re.sub(r'_+', '_', team).strip('_')
            normalized.append(team)
        
        # Join team names with vs
        match_key = "__vs__".join(normalized)
        
        return match_key
    
    async def fetch_page(self):
        """Fetch the webpage content using aiohttp"""
        try:
            session = await self.setup_session()
            async with session.get(self.url, headers=self.headers) as response:
                if response.status != 200:
                    logger.error(f"HTTP error {response.status}")
                    self.error_count += 1
                    return None
                
                html_content = await response.text()
                
                # Check if content has changed by comparing hash
                content_hash = hash(html_content)
                
                # If we've seen this exact page before, skip processing
                if content_hash in self.page_history:
                    logger.debug("Page content unchanged, skipping processing")
                    return None
                
                # Add to history
                self.page_history.append(content_hash)
                
                self.last_successful_fetch = time.time()
                self.backoff_time = 1  # Reset backoff on success
                return html_content
        except Exception as e:
            logger.error(f"Error fetching page: {str(e)}")
            self.error_count += 1
            return None
    
    async def extract_cricket_odds(self, html_content):
        """Extract cricket odds using BeautifulSoup"""
        if not html_content:
            return []
            
        matches = []
        try:
            # Run BeautifulSoup parsing in a thread pool to avoid blocking
            soup = await run_in_threadpool(
                lambda: BeautifulSoup(html_content, 'html.parser')
            )
            
            # Find cricket sections
            cricket_sections = soup.select('ion-list.inplay-item-list')
            
            for section in cricket_sections:
                # Check if this is the cricket section
                header_content = section.select_one('.inplay-item-list__header-content')
                if not header_content:
                    continue
                    
                header_text = header_content.text.lower()
                is_cricket_section = 'cricket' in header_text
                
                # If not explicitly cricket in text, check for cricket icon
                if not is_cricket_section:
                    cricket_icons = section.select('.inplay-content__logo-icon--cricket')
                    if not cricket_icons:
                        continue
                
                # Get all match items in this section
                match_items = section.select('.inplay-item')
                
                for item in match_items:
                    try:
                        # Extract team names
                        player_elems = item.select('.inplay-item__player span')
                        team1 = player_elems[0].text.strip() if len(player_elems) >= 1 else ""
                        team2 = player_elems[1].text.strip() if len(player_elems) > 1 else ""
                        
                        # Create a stable ID
                        stable_id = self._create_stable_id(team1, team2)
                        
                        # Initialize match data
                        match_data = {
                            'id': f"match_{stable_id}",
                            'timestamp': datetime.now().isoformat(),
                            'team1': team1,
                            'team2': team2
                        }
                        
                        # Extract date and time
                        date_elems = item.select('.date-content .inPlayDate-content__date')
                        time_elems = item.select('.date-content .inPlayDate-content__time')
                        
                        if date_elems and time_elems:
                            match_data['date'] = date_elems[0].text.strip()
                            match_data['time'] = time_elems[0].text.strip()
                        
                        # Extract current score
                        score_elem = item.select('.score-content:not(.empty)')
                        if score_elem:
                            score_spans = score_elem[0].select('span')
                            if score_spans:
                                match_data['score'] = [span.text.strip() for span in score_spans]
                                match_data['in_play'] = True
                        else:
                            match_data['in_play'] = False
                        
                        # Extract odds
                        odds = {'back': [], 'lay': []}
                        
                        # Back odds
                        back_buttons = item.select('.odd-button.back-color')
                        for i, button in enumerate(back_buttons):
                            price_elem = button.select_one('.odd-button__price')
                            volume_elem = button.select_one('.odd-button__volume')
                            
                            if price_elem and price_elem.text.strip() and price_elem.text.strip() != '-':
                                odds['back'].append({
                                    'position': i,
                                    'price': price_elem.text.strip(),
                                    'volume': volume_elem.text.strip() if volume_elem else None
                                })
                        
                        # Lay odds
                        lay_buttons = item.select('.odd-button.lay-color')
                        for i, button in enumerate(lay_buttons):
                            price_elem = button.select_one('.odd-button__price')
                            volume_elem = button.select_one('.odd-button__volume')
                            
                            if price_elem and price_elem.text.strip() and price_elem.text.strip() != '-':
                                odds['lay'].append({
                                    'position': i,
                                    'price': price_elem.text.strip(),
                                    'volume': volume_elem.text.strip() if volume_elem else None
                                })
                        
                        match_data['odds'] = odds
                        matches.append(match_data)
                    except Exception as e:
                        logger.debug(f"Error processing match: {str(e)}")
                        continue
            
            if matches:
                logger.info(f"Extracted {len(matches)} cricket matches")
                self.error_count = 0
            else:
                logger.warning("No cricket matches found")
                self.error_count += 1
            
            return matches
            
        except Exception as e:
            logger.error(f"Error extracting cricket odds: {str(e)}")
            self.error_count += 1
            return []
    
    async def _has_odds_changed(self, old_match, new_match):
        """Compare odds between old and new match data to detect changes"""
        # Check if score changed
        if old_match.get("score") != new_match.get("score"):
            return True
            
        # Check if in_play status changed
        if old_match.get("in_play") != new_match.get("in_play"):
            return True
        
        # Check for odds changes
        old_odds = old_match.get("odds", {})
        new_odds = new_match.get("odds", {})
        
        # Compare back odds
        old_back = old_odds.get("back", [])
        new_back = new_odds.get("back", [])
        
        if len(old_back) != len(new_back):
            return True
            
        for i, (old_odd, new_odd) in enumerate(zip(old_back, new_back)):
            if old_odd.get("price") != new_odd.get("price"):
                return True
            if old_odd.get("volume") != new_odd.get("volume"):
                return True
        
        # Compare lay odds
        old_lay = old_odds.get("lay", [])
        new_lay = new_odds.get("lay", [])
        
        if len(old_lay) != len(new_lay):
            return True
            
        for i, (old_odd, new_odd) in enumerate(zip(old_lay, new_lay)):
            if old_odd.get("price") != new_odd.get("price"):
                return True
            if old_odd.get("volume") != new_odd.get("volume"):
                return True
        
        return False
    
    async def update_global_state(self, new_matches):
        """Update the global state with new matches data"""
        try:
            changes_made = 0
            current_time = datetime.now().isoformat()
            
            with scraper_state["lock"]:
                # Create output data structure
                output_data = {
                    'timestamp': current_time,
                    'updated': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'matches': new_matches
                }
                
                # Advanced change detection
                old_matches = scraper_state["data"].get("matches", [])
                old_match_ids = {m.get("id"): m for m in old_matches}
                new_match_ids = {m.get("id"): m for m in new_matches}
                
                # Check for added or removed matches
                added_matches = set(new_match_ids.keys()) - set(old_match_ids.keys())
                removed_matches = set(old_match_ids.keys()) - set(new_match_ids.keys())
                initial_changes = len(added_matches) + len(removed_matches)
                changes_made += initial_changes
                
                # Check for updated odds in existing matches
                for match_id in set(old_match_ids.keys()) & set(new_match_ids.keys()):
                    old_match = old_match_ids[match_id]
                    new_match = new_match_ids[match_id]
                    
                    # Detailed check for specific changes
                    if await self._has_odds_changed(old_match, new_match):
                        changes_made += 1
                
                # Update global state
                scraper_state["data"] = output_data
                scraper_state["last_updated"] = current_time
                scraper_state["status"] = "running"
                scraper_state["changes_since_last_update"] = changes_made
                
                # Save data to file if changes were made
                if changes_made > 0:
                    await self._save_data_files(output_data)
                    logger.info(f"Data saved with {changes_made} changes")
                
                # Log when we extract data
                timestamp = datetime.now().strftime("%H:%M:%S")
                logger.info(f"Data updated at {timestamp} with {len(new_matches)} matches" + 
                           (f" ({changes_made} changes)" if changes_made > 0 else ""))
                
                # Clear response cache when data changes
                if changes_made > 0:
                    response_cache.clear()
                
                return True
        except Exception as e:
            logger.error(f"Error updating global state: {str(e)}")
            self.error_count += 1
            return False
    
    async def _save_data_files(self, output_data):
        """Save data to files asynchronously"""
        try:
            # Save the data to a temporary file first, then rename to avoid partial writes
            temp_file = f"{DATA_FILE}.tmp"
            
            # Run file I/O in a thread pool to avoid blocking
            await run_in_threadpool(
                lambda: self._write_json_file(temp_file, output_data)
            )
            
            # Atomic rename operation
            await run_in_threadpool(
                lambda: os.replace(temp_file, DATA_FILE)
            )
            
            logger.info("Data saved to disk")
            return True
        except Exception as e:
            logger.error(f"Error saving data file: {str(e)}")
            return False
    
    def _write_json_file(self, filename, data):
        """Write JSON data to file (called in thread pool)"""
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    
    async def run(self):
        """Run the scraper continuously"""
        with scraper_state["lock"]:
            scraper_state["is_running"] = True
            scraper_state["start_time"] = datetime.now()
            scraper_state["status"] = "starting"
        
        logger.info(f"Starting cricket odds scraper with {SCRAPE_INTERVAL} second interval")
        
        # Track successful extraction stats
        last_successful_extraction = None
        page_refresh_interval = 10  # Force full page refresh every 10 seconds
        
        try:
            # Main scraper loop
            iteration_counter = 0
            
            while scraper_state["is_running"]:
                try:
                    start_time = time.time()
                    iteration_counter += 1
                    
                    # Log heartbeat every 30 iterations
                    if iteration_counter % 30 == 0:
                        logger.info(f"Scraper heartbeat: iteration {iteration_counter}")
                    
                    # Fetch and extract data
                    html_content = await self.fetch_page()
                    
                    if html_content:
                        matches = await self.extract_cricket_odds(html_content)
                        if matches:
                            await self.update_global_state(matches)
                            last_successful_extraction = time.time()
                    
                    # Update error count in global state
                    with scraper_state["lock"]:
                        scraper_state["error_count"] = self.error_count
                        scraper_state["last_updated"] = datetime.now().isoformat()
                    
                    # Apply adaptive backoff if too many errors
                    if self.error_count > self.max_continuous_errors:
                        sleep_time = min(self.backoff_time, self.max_backoff)
                        logger.warning(f"Too many errors, backing off for {sleep_time}s")
                        self.backoff_time *= 2  # Exponential backoff
                        await asyncio.sleep(sleep_time)
                    else:
                        # Calculate precise sleep time to maintain interval
                        elapsed = time.time() - start_time
                        sleep_time = max(0, SCRAPE_INTERVAL - elapsed)
                        
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                        
                except Exception as e:
                    logger.error(f"Error in scraper loop: {str(e)}")
                    await asyncio.sleep(1)  # Short recovery sleep
                    
        except Exception as e:
            logger.error(f"Unexpected error in scraper: {str(e)}")
        finally:
            # Clean up
            if self.session and not self.session.closed:
                await self.session.close()
            
            with scraper_state["lock"]:
                scraper_state["is_running"] = False
                scraper_state["status"] = "stopped"

# Initialize and start the scraper
async def start_scraper():
    if not scraper_state["is_running"]:
        scraper = CricketOddsScraper()
        asyncio.create_task(scraper.run())
        logger.info("Scraper task started")
        return True
    else:
        return False

# On startup, start the scraper
@app.on_event("startup")
async def startup_event():
    """Start the application and initialize the scraper"""
    # Initialize scraper state
    scraper_state["start_time"] = datetime.now()
    
    # Start the scraper automatically
    if await start_scraper():
        logger.info("API started and scraper initialized successfully")
    else:
        logger.warning("API started but scraper was already running")
    
    # Start a task to monitor scraper health
    asyncio.create_task(monitor_scraper_health())

async def monitor_scraper_health():
    """Monitor scraper health and restart if needed"""
    while True:
        try:
            # Check if updates are happening
            with scraper_state["lock"]:
                last_updated = scraper_state.get("last_updated")
                is_running = scraper_state.get("is_running", False)
            
            if last_updated:
                last_updated_time = datetime.fromisoformat(last_updated)
                current_time = datetime.now()
                
                # If no updates for more than 30 seconds, restart scraper
                if (current_time - last_updated_time).total_seconds() > 30:
                    logger.warning("No updates for 30+ seconds. Restarting scraper.")
                    
                    # Stop the scraper if it's running
                    with scraper_state["lock"]:
                        scraper_state["is_running"] = False
                    
                    # Wait a moment for cleanup
                    await asyncio.sleep(5)
                    
                    # Start a new scraper
                    await start_scraper()
            
            # Check if scraper is marked as running but not actually updating
            elif not is_running:
                logger.warning("Scraper not running. Starting it.")
                await start_scraper()
                
            # Check again after 15 seconds
            await asyncio.sleep(15)
            
        except Exception as e:
            logger.error(f"Error in scraper health monitor: {e}")
            await asyncio.sleep(10)

# API Endpoints

@app.get("/", tags=["Root"])
async def root():
    """Root endpoint with API information"""
    return {
        "name": "Cricket Odds API",
        "version": "3.0.0",
        "description": "Optimized API for real-time cricket odds from betbhai.io",
        "endpoints": [
            {"path": "/matches", "description": "Get all cricket matches"},
            {"path": "/matches/{match_id}", "description": "Get a specific match by ID"},
            {"path": "/status", "description": "Get the scraper status"}
        ]
    }

@app.get("/matches", response_model=List[Match], tags=["Matches"])
@cached(ttl_seconds=2)
async def get_matches(
    team: Optional[str] = Query(None, description="Filter by team name"),
    in_play: Optional[bool] = Query(None, description="Filter by in-play status")
):
    """Get all cricket matches with optional filtering"""
    with scraper_state["lock"]:
        matches = scraper_state["data"].get("matches", [])
    
    # Apply filters if provided
    if team:
        team_lower = team.lower()
        matches = [
            m for m in matches 
            if (m.get("team1", "").lower().find(team_lower) != -1 or 
                m.get("team2", "").lower().find(team_lower) != -1)
        ]
    
    if in_play is not None:
        matches = [m for m in matches if m.get("in_play") == in_play]
    
    return matches

@app.get("/matches/{match_id}", tags=["Matches"])
@cached(ttl_seconds=2)
async def get_match(match_id: str):
    """Get a specific cricket match by ID"""
    with scraper_state["lock"]:
        matches = scraper_state["data"].get("matches", [])
    
    for match in matches:
        if match.get("id") == match_id:
            return match
    
    # Match not found
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Match with ID {match_id} not found"
    )

@app.get("/status", response_model=ScraperStatus, tags=["System"])
async def get_status():
    """Get the current status of the scraper"""
    with scraper_state["lock"]:
        uptime = (datetime.now() - scraper_state["start_time"]).total_seconds() if scraper_state["start_time"] else 0
        return {
            "status": scraper_state["status"],
            "last_updated": scraper_state["last_updated"],
            "matches_count": len(scraper_state["data"].get("matches", [])),
            "is_running": scraper_state["is_running"],
            "error_count": scraper_state["error_count"],
            "uptime_seconds": int(uptime),
            "changes_since_last_update": scraper_state.get("changes_since_last_update", 0)
        }

@app.get("/health", tags=["System"])
async def health_check():
    """Health check endpoint for monitoring"""
    with scraper_state["lock"]:
        is_healthy = scraper_state["is_running"]
        last_updated = scraper_state["last_updated"]
    
    if not is_healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "detail": "Scraper is not running"}
        )
    
    if last_updated:
        last_updated_time = datetime.fromisoformat(last_updated)
        current_time = datetime.now()
        
        # If no updates for more than 60 seconds, consider unhealthy
        if (current_time - last_updated_time).total_seconds() > 60:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unhealthy", "detail": "No recent updates"}
            )
    
    return {"status": "healthy"}

# On shutdown
@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown the application and stop the scraper"""
    # Stop the scraper if running
    with scraper_state["lock"]:
        scraper_state["is_running"] = False
        logger.info("API shutting down, stopping scraper")

if __name__ == "__main__":
    # Use the PORT environment variable provided by Render
    port = int(os.environ.get("PORT", 10000))
    
    # Start the uvicorn server
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
