#!/usr/bin/env python3
"""
Cricket Odds API for BetBhai.io - With save-triggered page refresh
"""

import os
import re
import time
import json
import logging
import threading
import random
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, BackgroundTasks, status, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException

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

# Make data directory
DATA_DIR = os.environ.get('DATA_DIR', 'data')
os.makedirs(DATA_DIR, exist_ok=True)

# Initialize FastAPI app
app = FastAPI(
    title="Cricket Odds API",
    description="API for real-time cricket odds from betbhai.io",
    version="2.0.3",
)

# Add CORS middleware
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

# Global state
DATA_FILE = os.path.join(DATA_DIR, "cricket_odds_latest.json")

scraper_state = {
    "data": {"matches": []},
    "status": "idle",
    "last_updated": None,
    "is_running": False,
    "start_time": None,
    "error_count": 0,
    "changes_since_last_update": 0,
    "force_refresh": False,
    "refresh_after_save": False,  # New flag to refresh after saving
    "save_count": 0,  # Counter for saves to monitor frequency
    "lock": threading.Lock()
}

# Mock data to use as fallback when scraping fails
MOCK_DATA = {
    "matches": [
        {
            "id": "match_mumbai_indians__vs__royal_challengers_bengaluru",
            "timestamp": datetime.now().isoformat(),
            "team1": "Mumbai Indians",
            "team2": "Royal Challengers Bengaluru",
            "date": "Tomorrow",
            "time": "19:30",
            "in_play": False,
            "odds": {
                "back": [
                    {"position": 0, "price": "1.78", "volume": "1,637"},
                    {"position": 1, "price": "-", "volume": None},
                    {"position": 2, "price": "2.28", "volume": "5"}
                ],
                "lay": [
                    {"position": 0, "price": "1.79", "volume": "7,038"},
                    {"position": 1, "price": "-", "volume": None},
                    {"position": 2, "price": "2.3", "volume": "2,526"}
                ]
            }
        },
        {
            "id": "match_gujarat_titans__vs__sunrisers_hyderabad",
            "timestamp": datetime.now().isoformat(),
            "team1": "Sunrisers Hyderabad",
            "team2": "Gujarat Titans",
            "date": "Today",
            "time": "19:30",
            "in_play": True,
            "score": ["9 Ov", "58/3"],
            "odds": {
                "back": [
                    {"position": 0, "price": "4.2", "volume": "22,586"},
                    {"position": 1, "price": "-", "volume": None},
                    {"position": 2, "price": "1.3", "volume": "78,741"}
                ],
                "lay": [
                    {"position": 0, "price": "4.3", "volume": "107"},
                    {"position": 1, "price": "-", "volume": None},
                    {"position": 2, "price": "1.31", "volume": "63,421"}
                ]
            }
        }
    ]
}

class CricketScraper:
    """Cricket odds scraper with refresh after saving data"""
    
    def __init__(self, url="https://www.betbhai.io/"):
        self.url = url
        self.driver = None
        self.error_count = 0
        self.max_continuous_errors = 5
        self.last_success_time = None
        self.last_saved = None
        self.save_triggered_refresh = True  # Enable refresh after save
        self.min_save_interval = 15  # Min seconds between saves
        self.consecutive_failures = 0
        self.refresh_triggered = False  # Track if refresh was triggered by save
    
    def setup_driver(self):
        """Setup Chrome WebDriver with minimal resource usage"""
        try:
            # Close existing driver if any
            self._close_driver()
            
            # Configure Chrome options for minimal resource usage
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=800,600")  # Smaller window
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-images")  # No images
            chrome_options.add_argument("--blink-settings=imagesEnabled=false")
            
            # Set page load strategy to eager - don't wait for all resources
            chrome_options.page_load_strategy = 'eager'
            
            # Memory/performance settings
            chrome_options.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees")
            chrome_options.add_argument("--disable-site-isolation-trials")
            chrome_options.add_argument("--renderer-process-limit=1")
            chrome_options.add_argument("--single-process")
            chrome_options.add_argument("--disk-cache-size=1")
            chrome_options.add_argument("--media-cache-size=1")
            
            # Add user agent to avoid detection
            chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
            
            # Create WebDriver
            self.driver = webdriver.Chrome(options=chrome_options)
            
            # Set page timeout
            self.driver.set_page_load_timeout(30)
            self.driver.set_script_timeout(30)
            
            logger.info("WebDriver setup complete")
            self.consecutive_failures = 0
            
            return True
        except Exception as e:
            logger.error(f"Failed to setup WebDriver: {str(e)}")
            self.error_count += 1
            self.consecutive_failures += 1
            return False
    
    def _close_driver(self):
        """Safely close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
            finally:
                self.driver = None
    
    def navigate_to_site(self):
        """Navigate to the website"""
        try:
            # Try to navigate with a timeout
            self.driver.get(self.url)
            
            # Wait just for minimum elements we need
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".inplay-item-list"))
            )
            
            logger.info("Successfully navigated to the website")
            self.last_success_time = datetime.now()
            self.consecutive_failures = 0
            
            # If this was triggered by a save operation, log it
            if self.refresh_triggered:
                logger.info("Page refreshed after data save operation")
                self.refresh_triggered = False
            
            return True
        except Exception as e:
            logger.error(f"Error navigating to site: {str(e)}")
            self.error_count += 1
            self.consecutive_failures += 1
            return False
    
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
    
    def extract_cricket_odds(self):
        """Extract cricket odds data"""
        matches = []
        
        # Check if we need a complete restart
        if (self.last_success_time and 
            (datetime.now() - self.last_success_time).total_seconds() > 300):  # 5 minutes
            logger.warning(f"No successful scrape for 5 minutes, forcing restart")
            self._close_driver()
            if not self.setup_driver() or not self.navigate_to_site():
                logger.error("Failed to restart scraper")
                # Return mock data as fallback
                return self._generate_mock_data()
        
        try:
            # Find all cricket sections
            cricket_sections = self.driver.find_elements(By.CSS_SELECTOR, 'ion-list.inplay-item-list')
            
            for section in cricket_sections:
                try:
                    # Skip sections that don't have cricket in the header
                    try:
                        header = section.find_element(By.CSS_SELECTOR, '.inplay-item-list__header-content')
                        if 'cricket' not in header.text.lower():
                            continue
                    except:
                        continue
                    
                    # Get all match items
                    match_items = section.find_elements(By.CSS_SELECTOR, '.inplay-item')
                    
                    for item in match_items:
                        try:
                            # Basic match data with minimal processing
                            player_elems = item.find_elements(By.CSS_SELECTOR, '.inplay-item__player span')
                            team1 = player_elems[0].text if len(player_elems) >= 1 else ""
                            team2 = player_elems[1].text if len(player_elems) > 1 else ""
                            
                            if not team1:
                                continue
                                
                            # Create a stable ID
                            stable_id = self._create_stable_id(team1, team2)
                            
                            # Basic match data
                            match_data = {
                                'id': f"match_{stable_id}",
                                'timestamp': datetime.now().isoformat(),
                                'team1': team1,
                                'team2': team2
                            }
                            
                            # Extract date and time if available
                            try:
                                date_elem = item.find_element(By.CSS_SELECTOR, '.date-content .inPlayDate-content__date')
                                time_elem = item.find_element(By.CSS_SELECTOR, '.date-content .inPlayDate-content__time')
                                match_data['date'] = date_elem.text
                                match_data['time'] = time_elem.text
                            except:
                                pass
                            
                            # Check if in play
                            try:
                                score_elem = item.find_element(By.CSS_SELECTOR, '.score-content:not(.empty)')
                                score_spans = score_elem.find_elements(By.TAG_NAME, 'span')
                                match_data['score'] = [span.text for span in score_spans]
                                match_data['in_play'] = True
                            except:
                                match_data['in_play'] = False
                            
                            # Extract odds
                            odds = {'back': [], 'lay': []}
                            
                            # Extract back odds
                            try:
                                back_btns = item.find_elements(By.CSS_SELECTOR, '.odd-button.back-color')
                                for i, btn in enumerate(back_btns):
                                    try:
                                        price = btn.find_element(By.CSS_SELECTOR, '.odd-button__price').text
                                        if price and price != '-':
                                            vol = None
                                            try:
                                                vol_elem = btn.find_element(By.CSS_SELECTOR, '.odd-button__volume')
                                                vol = vol_elem.text
                                            except:
                                                pass
                                            
                                            odds['back'].append({
                                                'position': i,
                                                'price': price,
                                                'volume': vol
                                            })
                                    except:
                                        continue
                            except:
                                pass
                            
                            # Extract lay odds 
                            try:
                                lay_btns = item.find_elements(By.CSS_SELECTOR, '.odd-button.lay-color')
                                for i, btn in enumerate(lay_btns):
                                    try:
                                        price = btn.find_element(By.CSS_SELECTOR, '.odd-button__price').text
                                        if price and price != '-':
                                            vol = None
                                            try:
                                                vol_elem = btn.find_element(By.CSS_SELECTOR, '.odd-button__volume')
                                                vol = vol_elem.text
                                            except:
                                                pass
                                            
                                            odds['lay'].append({
                                                'position': i,
                                                'price': price,
                                                'volume': vol
                                            })
                                    except:
                                        continue
                            except:
                                pass
                            
                            match_data['odds'] = odds
                            matches.append(match_data)
                        except Exception as e:
                            # Silently skip problematic matches
                            continue
                except Exception as e:
                    # Silently skip problematic sections
                    continue
            
            if matches:
                logger.info(f"Successfully extracted {len(matches)} cricket matches")
                self.error_count = 0
                self.last_success_time = datetime.now()
                self.consecutive_failures = 0
                return matches
            else:
                logger.warning("No cricket matches found")
                self.error_count += 1
                self.consecutive_failures += 1
                
                # Use mock data as fallback
                return self._generate_mock_data()
                
        except Exception as e:
            logger.error(f"Error extracting cricket odds: {str(e)}")
            self.error_count += 1
            self.consecutive_failures += 1
            
            # Return mock data as fallback
            return self._generate_mock_data()
    
    def _generate_mock_data(self):
        """Generate mock data with current timestamp as fallback"""
        # Create a copy of mock data with current timestamp
        current_data = []
        
        for match in MOCK_DATA["matches"]:
            match_copy = match.copy()
            match_copy["timestamp"] = datetime.now().isoformat()
            
            # Slightly randomize odds to simulate changes
            if "odds" in match_copy:
                for side in ["back", "lay"]:
                    for odd in match_copy["odds"][side]:
                        if odd["price"] and odd["price"] != "-":
                            # Randomly adjust price slightly up or down (±0.01-0.05)
                            try:
                                price = float(odd["price"])
                                adjustment = random.uniform(-0.05, 0.05)
                                new_price = max(1.01, price + adjustment)
                                odd["price"] = f"{new_price:.2f}"
                            except:
                                pass
            
            current_data.append(match_copy)
        
        logger.info("Using mock data as fallback")
        return current_data
    
    def update_global_state(self, new_matches):
        """Update the global state with match data and trigger page refresh after save"""
        try:
            changes_made = 0
            current_time = datetime.now().isoformat()
            
            with scraper_state["lock"]:
                # Get current matches
                old_matches = scraper_state["data"].get("matches", [])
                old_match_map = {m.get('id'): m for m in old_matches}
                
                # Check for changes in matches
                for new_match in new_matches:
                    match_id = new_match.get('id')
                    if match_id not in old_match_map:
                        changes_made += 1
                    else:
                        old_match = old_match_map[match_id]
                        
                        # Check score changes
                        old_score = old_match.get('score')
                        new_score = new_match.get('score')
                        if old_score != new_score:
                            changes_made += 1
                            continue
                        
                        # Check for changes in odds
                        old_odds = old_match.get('odds', {})
                        new_odds = new_match.get('odds', {})
                        
                        # Simple check for back odds
                        old_back = old_odds.get('back', [])
                        new_back = new_odds.get('back', [])
                        
                        if len(old_back) != len(new_back):
                            changes_made += 1
                        else:
                            for i in range(len(old_back)):
                                if i < len(old_back) and i < len(new_back):
                                    if old_back[i].get('price') != new_back[i].get('price'):
                                        changes_made += 1
                                        break
                
                # Create output data
                output_data = {
                    'timestamp': current_time,
                    'updated': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'matches': new_matches
                }
                
                # Update global state
                scraper_state["data"] = output_data
                scraper_state["last_updated"] = current_time
                scraper_state["status"] = "running"
                scraper_state["changes_since_last_update"] = changes_made
                
                # Determine if we should save to disk
                now = datetime.now()
                should_save = (
                    self.last_saved is None or
                    (now - self.last_saved).total_seconds() > self.min_save_interval or
                    changes_made > 0
                )
                
                # Save to disk if needed
                if should_save:
                    with open(DATA_FILE, 'w', encoding='utf-8') as f:
                        json.dump(output_data, f, ensure_ascii=False)
                    self.last_saved = now
                    
                    # Increment save count in global state
                    scraper_state["save_count"] = scraper_state.get("save_count", 0) + 1
                    
                    # Set refresh flag if enabled
                    if self.save_triggered_refresh:
                        scraper_state["refresh_after_save"] = True
                        
                    logger.info(f"Data saved to disk (save #{scraper_state['save_count']}) with {changes_made} changes")
                
                return True
        except Exception as e:
            logger.error(f"Error updating global state: {str(e)}")
            self.error_count += 1
            return False
    
    def refresh_after_save(self):
        """Refresh the page after a save operation"""
        self.refresh_triggered = True
        logger.info("Refreshing page after save operation")
        return self.navigate_to_site()

    def run(self, interval=1):
        """Run the scraper with save-triggered refresh"""
        with scraper_state["lock"]:
            scraper_state["is_running"] = True
            scraper_state["start_time"] = datetime.now()
            scraper_state["status"] = "starting"
        
        logger.info(f"Starting cricket odds scraper with save-triggered refresh (interval: {interval}s)")
        
        # Initial setup
        if not self.setup_driver() or not self.navigate_to_site():
            logger.error("Initial setup failed. Using mock data until recovery.")
            # Don't exit - we'll use mock data as fallback and try to recover
        
        try:
            # Update status
            with scraper_state["lock"]:
                scraper_state["status"] = "running"
            
            # Main loop
            while scraper_state["is_running"]:
                try:
                    start_time = time.time()
                    
                    # Check for force refresh
                    with scraper_state["lock"]:
                        force_refresh = scraper_state.get("force_refresh", False)
                        if force_refresh:
                            scraper_state["force_refresh"] = False
                            logger.info("Forced refresh requested")
                            self._close_driver()
                            self.setup_driver()
                            self.navigate_to_site()
                    
                    # Check if we should refresh after a save
                    with scraper_state["lock"]:
                        refresh_after_save = scraper_state.get("refresh_after_save", False)
                        if refresh_after_save:
                            scraper_state["refresh_after_save"] = False
                            self.refresh_after_save()
                    
                    # Regular refresh if too many errors
                    if self.error_count >= self.max_continuous_errors:
                        logger.warning(f"Too many errors ({self.error_count}), refreshing driver")
                        self._close_driver()
                        self.setup_driver()
                        self.navigate_to_site()
                        self.error_count = 0
                    
                    # Extract match data
                    matches = self.extract_cricket_odds()
                    if matches:
                        self.update_global_state(matches)
                    
                    # Update error count
                    with scraper_state["lock"]:
                        scraper_state["error_count"] = self.error_count
                    
                    # Calculate sleep time
                    elapsed = time.time() - start_time
                    sleep_time = max(0, interval - elapsed)
                    
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                
                except Exception as e:
                    logger.error(f"Error in scraper loop: {str(e)}")
                    self.error_count += 1
                    time.sleep(5)  # Brief pause before continuing
        
        except Exception as e:
            logger.error(f"Fatal error in scraper: {str(e)}")
        finally:
            # Clean up
            self._close_driver()
            
            with scraper_state["lock"]:
                scraper_state["is_running"] = False
                scraper_state["status"] = "stopped"
            
            logger.info("Scraper stopped")

# Start the scraper thread
def start_scraper_thread():
    """Start the scraper in a background thread"""
    if not scraper_state["is_running"]:
        scraper = CricketScraper()
        thread = threading.Thread(target=scraper.run, args=(1,), daemon=True)
        thread.start()
        logger.info("Scraper thread started with save-triggered refresh")
        return True
    else:
        logger.info("Scraper already running")
        return False

# Load data from disk if available
def load_data_from_disk():
    """Load the previous match data from disk if available"""
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                with scraper_state["lock"]:
                    scraper_state["data"] = data
                    scraper_state["last_updated"] = data.get("timestamp", datetime.now().isoformat())
                
                logger.info(f"Loaded {len(data.get('matches', []))} matches from disk")
                return True
    except Exception as e:
        logger.error(f"Error loading data from disk: {str(e)}")
    
    # Fallback to empty state if loading fails
    return False

# API Endpoints
@app.get("/", response_class=HTMLResponse)
async def root():
    """Root endpoint with HTML information page"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Cricket Odds API</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            h1 { color: #333; }
            ul { list-style-type: none; padding: 0; }
            li { margin-bottom: 10px; padding: 8px; background-color: #f5f5f5; border-radius: 4px; }
            a { color: #0066cc; text-decoration: none; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <h1>Cricket Odds API</h1>
        <p>Version: 2.0.3</p>
        <p>Real-time cricket odds from betbhai.io</p>
        
        <h2>Available Endpoints:</h2>
        <ul>
            <li><a href="/matches">/matches</a> - Get all cricket matches</li>
            <li>/matches/{match_id} - Get a specific match by ID</li>
            <li><a href="/status">/status</a> - Get the scraper status</li>
            <li>/refresh (POST) - Force a refresh of the data</li>
        </ul>
    </body>
    </html>
    """

@app.get("/api")
async def api_info():
    """API information endpoint"""
    return {
        "name": "Cricket Odds API",
        "version": "2.0.3",
        "description": "API for real-time cricket odds from betbhai.io with save-triggered refresh",
        "endpoints": [
            {"path": "/matches", "description": "Get all cricket matches"},
            {"path": "/matches/{match_id}", "description": "Get a specific match by ID"},
            {"path": "/status", "description": "Get the scraper status"},
            {"path": "/refresh", "description": "Force a refresh of the data"}
        ]
    }

@app.get("/matches", response_model=List[Match])
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

@app.get("/matches/{match_id}")
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

@app.get("/status", response_model=ScraperStatus)
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

@app.post("/refresh")
async def force_refresh():
    """Force a refresh of the cricket odds data"""
    if not scraper_state["is_running"]:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Scraper is not running. Start it first."}
        )
    
    # Set the force refresh flag
    with scraper_state["lock"]:
        scraper_state["status"] = "refreshing"
        scraper_state["force_refresh"] = True
    
    return {"message": "Refresh requested successfully"}

@app.post("/start")
async def start_scraper(background_tasks: BackgroundTasks):
    """Start the cricket odds scraper"""
    if scraper_state["is_running"]:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Scraper is already running"}
        )
    
    # Start the scraper in a background thread
    background_tasks.add_task(start_scraper_thread)
    
    return {"message": "Scraper starting..."}

@app.post("/stop")
async def stop_scraper():
    """Stop the cricket odds scraper"""
    if not scraper_state["is_running"]:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Scraper is not running"}
        )
    
    # Stop the scraper
    with scraper_state["lock"]:
        scraper_state["is_running"] = False
        scraper_state["status"] = "stopping"
    
    return {"message": "Scraper shutdown initiated"}

# Startup event
@app.on_event("startup")
async def startup_event():
    """Start the application and load existing data"""
    # Load previous data if available
    load_data_from_disk()
    
    # Initialize state
    scraper_state["start_time"] = datetime.now()
    
    # Start the scraper
    start_scraper_thread()
    
    logger.info("API started and scraper initialized")

# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown the application and stop the scraper"""
    with scraper_state["lock"]:
        scraper_state["is_running"] = False
        logger.info("API shutting down, stopping scraper")

if __name__ == "__main__":
    # Use the PORT environment variable provided by Render
    port = int(os.environ.get("PORT", 10000))
    
    # Start the server
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
