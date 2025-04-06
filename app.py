"""
Cricket Odds API for BetBhai.io - Optimized for Render Free Tier
With 1-second update frequency and reduced resource usage
"""

import os
import re
import time
import json
import logging
import threading
import uvicorn
from typing import List, Dict, Any, Optional, Union
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, status, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    WebDriverException, 
    TimeoutException, 
    NoSuchElementException, 
    StaleElementReferenceException
)

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

# Determine if running in production environment
IS_PRODUCTION = os.environ.get('RENDER', False)

# Make data directory
DATA_DIR = os.environ.get('DATA_DIR', 'data')
os.makedirs(DATA_DIR, exist_ok=True)

# File write frequency (only write to disk every X seconds to reduce I/O)
FILE_WRITE_INTERVAL = 60  # Write to disk once per minute instead of every update

# Initialize FastAPI app
app = FastAPI(
    title="Cricket Odds API",
    description="API for real-time cricket odds from betbhai.io",
    version="2.0.1",
)

# Add CORS middleware to allow cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify the domains instead of "*"
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

class MatchUpdate(BaseModel):
    timestamp: str
    odds_changed: bool = False
    score_changed: bool = False
    status_changed: bool = False

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
ID_MAPPING_FILE = os.path.join(DATA_DIR, "cricket_match_id_mapping.json")

scraper_state = {
    "data": {"matches": []},
    "status": "idle",
    "last_updated": None,
    "is_running": False,
    "start_time": None,
    "error_count": 0,
    "changes_since_last_update": 0,
    "id_mapping": {},  # Maps legacy IDs to current stable IDs
    "match_history": {},  # Tracks changes for each match
    "lock": threading.Lock(),
    "last_file_write": None  # Track when we last wrote to disk
}

class CricketOddsScraper:
    """Optimized scraper for extracting cricket odds from betbhai.io"""
    
    def __init__(self, url="https://www.betbhai.io/"):
        self.url = url
        self.driver = None
        self.retry_count = 0
        self.max_retries = 5
        self.error_count = 0
        self.max_continuous_errors = 10
        self.force_refresh = False
        # Use a smaller page refresh interval (every ~10 seconds instead of 30)
        self.max_extractions_before_refresh = 10
        # Keep track of the page load time to optimize scraping
        self.page_load_time = 0
    
    def setup_driver(self):
        """Set up the Selenium WebDriver with optimized options for Render"""
        try:
            # Close existing driver if any
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
            
            # Configure Chrome options with extreme memory optimization for Render
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            # Use a smaller window size to reduce memory usage
            chrome_options.add_argument("--window-size=1280,720")
            # Disable images to reduce bandwidth and memory
            chrome_options.add_argument("--disable-images")
            # Disable extensions
            chrome_options.add_argument("--disable-extensions")
            # Disable JavaScript JIT to reduce memory
            chrome_options.add_argument("--js-flags=--noopt")
            # Disable features that consume memory
            chrome_options.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees")
            
            # Set lower process limit
            chrome_options.add_argument("--renderer-process-limit=1")
            chrome_options.add_argument("--single-process")
            
            # Add user agent to avoid detection
            chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
            
            # Try to create WebDriver with direct ChromeDriver path
            try:
                logger.info("Trying with direct ChromeDriver path")
                service = Service(executable_path="/usr/local/bin/chromedriver")
                self.driver = webdriver.Chrome(service=service, options=chrome_options)
                logger.info("Successfully created WebDriver with direct path")
                self.retry_count = 0
                return True
            except Exception as e:
                logger.warning(f"Direct path attempt failed: {e}")
            
            # Try with default system-wide ChromeDriver
            try:
                logger.info("Trying with system-wide ChromeDriver")
                self.driver = webdriver.Chrome(options=chrome_options)
                logger.info("Successfully created WebDriver with system-wide ChromeDriver")
                self.retry_count = 0
                return True
            except Exception as e:
                logger.warning(f"System-wide ChromeDriver attempt failed: {e}")
            
            # All attempts failed
            self.retry_count += 1
            self.error_count += 1
            if self.retry_count < self.max_retries:
                logger.info(f"Retrying driver setup (attempt {self.retry_count}/{self.max_retries})...")
                time.sleep(2)  # Reduced sleep time
                return self.setup_driver()
            return False
        except Exception as e:
            logger.error(f"Error initializing WebDriver: {e}")
            self.retry_count += 1
            self.error_count += 1
            if self.retry_count < self.max_retries:
                logger.info(f"Retrying driver setup (attempt {self.retry_count}/{self.max_retries})...")
                time.sleep(2)  # Reduced sleep time
                return self.setup_driver()
            return False
    
    def navigate_to_site(self):
        """Navigate to the website and wait for it to load - with timing to optimize"""
        start_time = time.time()
        try:
            self.driver.get(self.url)
            # Wait for the cricket section to load with a shorter timeout
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".inplay-item-list"))
            )
            end_time = time.time()
            self.page_load_time = end_time - start_time
            logger.info(f"Successfully navigated to the website in {self.page_load_time:.2f} seconds")
            return True
        except TimeoutException:
            logger.error("Timeout while loading the website")
            self.error_count += 1
            return False
        except WebDriverException as e:
            logger.error(f"WebDriver error while navigating: {e}")
            self.error_count += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error while navigating: {e}")
            self.error_count += 1
            return False
    
    def _create_stable_id(self, team1: str, team2: str) -> str:
        """Create a stable ID based on team names"""
        if not team1:
            return "unknown_match"
        
        # Sort team names for consistency if both exist
        teams = sorted([team1, team2]) if team2 and team1 != team2 else [team1]
        
        # Normalize team names - remove spaces, special characters, etc.
        normalized = []
        for team in teams:
            # Convert to lowercase and replace non-alphanumeric with underscore
            team = "".join(c.lower() if c.isalnum() else '_' for c in team)
            # Remove consecutive underscores and trim
            team = re.sub(r'_+', '_', team).strip('_')
            normalized.append(team)
        
        # Join team names with vs
        match_key = "__vs__".join(normalized)
        
        return match_key
    
    def extract_cricket_odds(self):
        """Extract cricket odds data from the loaded page with optimized selectors"""
        matches = []
        
        try:
            # Find all cricket match items - use a single specific selector for cricket section
            cricket_sections = self.driver.find_elements(By.CSS_SELECTOR, 'ion-list.inplay-item-list')
            
            for section in cricket_sections:
                # Use a more efficient selector to find cricket section
                cricket_headers = section.find_elements(By.CSS_SELECTOR, '.inplay-item-list__header-logo:contains("Cricket"), .inplay-content__logo-icon--cricket')
                
                if not cricket_headers:
                    continue
                        
                # Get all match items in this section directly
                match_items = section.find_elements(By.CSS_SELECTOR, '.inplay-item')
                
                for item in match_items:
                    try:
                        # Extract team names with optimized selectors
                        player_elems = item.find_elements(By.CSS_SELECTOR, '.inplay-item__player span')
                        team1 = player_elems[0].text if len(player_elems) >= 1 else ""
                        team2 = player_elems[1].text if len(player_elems) > 1 else ""

                        # Create a stable ID based on team names
                        stable_id = self._create_stable_id(team1, team2)
                        
                        # Initialize match data with stable ID
                        match_data = {
                            'id': f"match_{stable_id}",
                            'timestamp': datetime.now().isoformat(),
                            'team1': team1,
                            'team2': team2
                        }
                        
                        # Extract date and time with direct selectors
                        date_elems = item.find_elements(By.CSS_SELECTOR, '.date-content .inPlayDate-content__date')
                        time_elems = item.find_elements(By.CSS_SELECTOR, '.date-content .inPlayDate-content__time')
                        
                        if date_elems and time_elems:
                            match_data['date'] = date_elems[0].text
                            match_data['time'] = time_elems[0].text
                        
                        # Extract current score if available - optimized selector
                        score_elem = item.find_elements(By.CSS_SELECTOR, '.score-content:not(.empty)')
                        if score_elem:
                            score_spans = score_elem[0].find_elements(By.TAG_NAME, 'span')
                            if score_spans:
                                match_data['score'] = [span.text for span in score_spans]
                                match_data['in_play'] = True
                        else:
                            match_data['in_play'] = False
                        
                        # Extract odds with optimized structure
                        odds = {'back': [], 'lay': []}
                        
                        # Back odds - get all at once with specific selector
                        back_buttons = item.find_elements(By.CSS_SELECTOR, '.odd-button.back-color')
                        for i, button in enumerate(back_buttons):
                            price_elem = button.find_elements(By.CSS_SELECTOR, '.odd-button__price')
                            volume_elem = button.find_elements(By.CSS_SELECTOR, '.odd-button__volume')
                            
                            if price_elem and price_elem[0].text and price_elem[0].text != '-':
                                odds['back'].append({
                                    'position': i,
                                    'price': price_elem[0].text,
                                    'volume': volume_elem[0].text if volume_elem else None
                                })
                        
                        # Lay odds - get all at once with specific selector
                        lay_buttons = item.find_elements(By.CSS_SELECTOR, '.odd-button.lay-color')
                        for i, button in enumerate(lay_buttons):
                            price_elem = button.find_elements(By.CSS_SELECTOR, '.odd-button__price')
                            volume_elem = button.find_elements(By.CSS_SELECTOR, '.odd-button__volume')
                            
                            if price_elem and price_elem[0].text and price_elem[0].text != '-':
                                odds['lay'].append({
                                    'position': i,
                                    'price': price_elem[0].text,
                                    'volume': volume_elem[0].text if volume_elem else None
                                })
                        
                        match_data['odds'] = odds
                        matches.append(match_data)
                    except (StaleElementReferenceException, NoSuchElementException, IndexError) as e:
                        # Handle exceptions without logging every time to reduce log size
                        pass
            
            if matches:
                logger.info(f"Extracted {len(matches)} cricket matches")
                # Reset error count on successful extraction
                self.error_count = 0
            else:
                logger.warning("No cricket matches found")
                self.error_count += 1
            
            return matches
            
        except Exception as e:
            logger.error(f"Error extracting cricket odds: {e}")
            self.error_count += 1
            return []
    
    def _detect_changes(self, old_match: Dict[str, Any], new_match: Dict[str, Any]) -> Dict[str, bool]:
        """Detect specific changes between two match objects - simplified for performance"""
        changes = {
            "odds_changed": False,
            "score_changed": False,
            "status_changed": False
        }
        
        # Check for status change
        if old_match.get('in_play') != new_match.get('in_play'):
            changes["status_changed"] = True
        
        # Check for score changes
        old_score = old_match.get('score')
        new_score = new_match.get('score')
        if old_score != new_score:
            changes["score_changed"] = True
        
        # Check if odds changed - optimized comparison
        old_odds = old_match.get('odds', {})
        new_odds = new_match.get('odds', {})
        
        # Simple check for back odds
        old_back = sorted(old_odds.get('back', []), key=lambda x: x.get('position', 0))
        new_back = sorted(new_odds.get('back', []), key=lambda x: x.get('position', 0))
        
        if len(old_back) != len(new_back):
            changes["odds_changed"] = True
        else:
            for i, (old_item, new_item) in enumerate(zip(old_back, new_back)):
                if old_item.get('price') != new_item.get('price'):
                    changes["odds_changed"] = True
                    break
        
        # Only check lay odds if back odds haven't changed
        if not changes["odds_changed"]:
            old_lay = sorted(old_odds.get('lay', []), key=lambda x: x.get('position', 0))
            new_lay = sorted(new_odds.get('lay', []), key=lambda x: x.get('position', 0))
            
            if len(old_lay) != len(new_lay):
                changes["odds_changed"] = True
            else:
                for i, (old_item, new_item) in enumerate(zip(old_lay, new_lay)):
                    if old_item.get('price') != new_item.get('price'):
                        changes["odds_changed"] = True
                        break
        
        return changes
    
    def update_global_state(self, new_matches):
        """Update the global state with new matches data - minimized file I/O"""
        try:
            changes_made = 0
            current_time = datetime.now().isoformat()
            
            with scraper_state["lock"]:
                # Get current matches and ID mapping
                current_matches = scraper_state["data"].get("matches", [])
                id_mapping = scraper_state.get("id_mapping", {})
                match_history = scraper_state.get("match_history", {})
                
                # Build a dictionary of current matches by ID for faster lookup
                current_matches_by_id = {m.get('id'): m for m in current_matches}
                
                # Create a mapping from team combinations to match IDs
                team_to_id_map = {}
                for match in current_matches:
                    team1 = match.get('team1', '')
                    team2 = match.get('team2', '')
                    if team1 or team2:  # Only map if at least one team is present
                        key = self._create_stable_id(team1, team2)
                        team_to_id_map[key] = match.get('id')
                
                # Process new matches
                updated_matches = []
                processed_ids = set()
                
                for new_match in new_matches:
                    # Extract info for matching
                    team1 = new_match.get('team1', '')
                    team2 = new_match.get('team2', '')
                    match_id = new_match.get('id')
                    stable_key = self._create_stable_id(team1, team2)
                    
                    # Find current match by ID or team combination
                    current_match = None
                    
                    # First try to find by ID
                    if match_id in current_matches_by_id:
                        current_match = current_matches_by_id[match_id]
                    
                    # If not found by ID, try by team combination
                    elif stable_key in team_to_id_map:
                        current_id = team_to_id_map[stable_key]
                        if current_id in current_matches_by_id:
                            current_match = current_matches_by_id[current_id]
                            # Update the ID mapping to point legacy ID to current stable ID
                            id_mapping[current_id] = match_id
                    
                    if current_match:
                        # Fast check for changes in odds (most frequent change type)
                        if current_match.get('odds') != new_match.get('odds'):
                            # Detect specific changes
                            changes = self._detect_changes(current_match, new_match)
                            
                            # Preserve the original ID but update content
                            new_match['id'] = current_match['id']
                            updated_matches.append(new_match)
                            changes_made += 1
                            
                            # Record change history
                            match_history.setdefault(new_match['id'], []).append({
                                'timestamp': current_time,
                                'odds_changed': changes['odds_changed'],
                                'score_changed': changes['score_changed'],
                                'status_changed': changes['status_changed']
                            })
                        else:
                            # No changes, keep current version
                            updated_matches.append(current_match)
                        
                        processed_ids.add(current_match['id'])
                    else:
                        # This is a new match
                        updated_matches.append(new_match)
                        changes_made += 1
                        
                        # Add to match history
                        match_history.setdefault(new_match['id'], []).append({
                            'timestamp': current_time,
                            'odds_changed': True,  # New match always has "new" odds
                            'score_changed': False,
                            'status_changed': False
                        })
                
                # Create output data structure
                output_data = {
                    'timestamp': current_time,
                    'updated': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'matches': updated_matches
                }
                
                # Update global state
                scraper_state["data"] = output_data
                scraper_state["last_updated"] = current_time
                scraper_state["status"] = "running"
                scraper_state["id_mapping"] = id_mapping
                scraper_state["match_history"] = match_history
                scraper_state["changes_since_last_update"] = changes_made
                
                # Only save data to files periodically to reduce I/O
                now = datetime.now()
                last_write = scraper_state.get("last_file_write")
                
                if (last_write is None or 
                    (now - last_write).total_seconds() > FILE_WRITE_INTERVAL or 
                    changes_made > 5):  # Write immediately if many changes
                    self._save_data_files(output_data, id_mapping)
                    scraper_state["last_file_write"] = now
                
                if changes_made > 0:
                    logger.info(f"Data updated with {changes_made} changes ({len(updated_matches)} matches)")
                return True
        except Exception as e:
            logger.error(f"Error updating global state: {e}")
            self.error_count += 1
            return False
    
    def _save_data_files(self, output_data, id_mapping):
        """Save data to files with error handling - only called periodically"""
        try:
            # Save the main data file
            temp_data_file = f"{DATA_FILE}.tmp"
            with open(temp_data_file, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, ensure_ascii=False)
            
            # Atomic rename to prevent corruption
            os.replace(temp_data_file, DATA_FILE)
            
            # Save ID mapping file
            temp_id_file = f"{ID_MAPPING_FILE}.tmp"
            with open(temp_id_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'updated': datetime.now().isoformat(),
                    'mapping': id_mapping
                }, f, ensure_ascii=False)
            
            # Atomic rename
            os.replace(temp_id_file, ID_MAPPING_FILE)
            
            logger.info("Data files saved to disk")
            return True
        except Exception as e:
            logger.error(f"Error saving data files: {e}")
            return False
    
    def run(self, interval=1):
        """Run the scraper every 'interval' seconds - optimized for 1-second updates"""
        # Update scraper state
        with scraper_state["lock"]:
            scraper_state["is_running"] = True
            scraper_state["start_time"] = datetime.now()
            scraper_state["status"] = "starting"
        
        logger.info(f"Starting cricket odds scraper with {interval} second intervals")
        
        if not self.setup_driver():
            logger.error("Failed to set up WebDriver. Exiting.")
            with scraper_state["lock"]:
                scraper_state["is_running"] = False
                scraper_state["status"] = "failed"
            return
        
        try:
            refresh_count = 0
            page_refresh_count = 0
            
            # Navigate to the site initially
            if not self.navigate_to_site():
                logger.error("Failed to navigate to the website. Retrying setup...")
                if not self.setup_driver() or not self.navigate_to_site():
                    logger.error("Still failed to navigate. Exiting.")
                    with scraper_state["lock"]:
                        scraper_state["is_running"] = False
                        scraper_state["status"] = "failed"
                    return
            
            # Update status to running
            with scraper_state["lock"]:
                scraper_state["status"] = "running"
            
            while scraper_state["is_running"]:
                start_time = time.time()
                
                # Check if we need to force refresh
                with scraper_state["lock"]:
                    force_refresh = getattr(self, 'force_refresh', False)
                    if force_refresh:
                        self.force_refresh = False
                
                # Check if we've had too many continuous errors
                if self.error_count >= self.max_continuous_errors:
                    logger.error(f"Reached maximum continuous errors ({self.max_continuous_errors}). Resetting driver...")
                    if not self.setup_driver() or not self.navigate_to_site():
                        logger.error("Driver reset failed. Waiting before retrying...")
                        time.sleep(15)  # Wait longer before retrying
                        continue
                    self.error_count = 0
                    refresh_count = 0
                    page_refresh_count = 0
                
                # Check if we need to refresh the page
                if refresh_count >= self.max_extractions_before_refresh or force_refresh:
                    # Instead of completely refreshing, can we just refresh the DOM?
                    page_refresh_count += 1
                    
                    if page_refresh_count >= 3:  # Complete page refresh every 3 DOM refreshes
                        logger.info("Performing complete page refresh")
                        if not self.navigate_to_site():
                            logger.warning("Page refresh failed, attempting to reset driver")
                            if not self.setup_driver() or not self.navigate_to_site():
                                logger.error("Driver reset failed. Waiting before retrying...")
                                time.sleep(15)
                                continue
                        page_refresh_count = 0
                    else:
                        # Try lightweight refresh first using JavaScript
                        try:
                            self.driver.execute_script("document.querySelector('.inplay-item-list').innerHTML = '';")
                            time.sleep(0.1)  # Brief pause
                            self.driver.execute_script("location.reload(false);")  # false means use cache
                            # Wait for content to reload
                            WebDriverWait(self.driver, 5).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, ".inplay-item-list"))
                            )
                            logger.info("Performed lightweight DOM refresh")
                        except Exception as e:
                            logger.warning(f"Lightweight refresh failed: {e}")
                            # If lightweight refresh fails, fallback to regular refresh
                            if not self.navigate_to_site():
                                logger.warning("Fallback refresh failed")
                                time.sleep(2)
                    
                    refresh_count = 0
                
                # Extract and update the data
                matches = self.extract_cricket_odds()
                if matches:
                    self.update_global_state(matches)
                
                refresh_count += 1
                
                # Update error count in global state
                with scraper_state["lock"]:
                    scraper_state["error_count"] = self.error_count
                
                # Calculate sleep time to maintain the interval
                elapsed = time.time() - start_time
                sleep_time = max(0, interval - elapsed)
                
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            logger.info("Scraper stopped by user")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            # Clean up
            try:
                if self.driver:
                    self.driver.quit()
                    logger.info("WebDriver closed")
            except:
                pass
            
            # Update scraper state
            with scraper_state["lock"]:
                scraper_state["is_running"] = False
                scraper_state["status"] = "stopped"

# Create a function to start the scraper in a background thread
def start_scraper_thread():
    if not scraper_state["is_running"]:
        # Create and start the thread
        scraper = CricketOddsScraper()
        thread = threading.Thread(target=scraper.run, args=(1,), daemon=True)  # Use 1-second interval
        thread.start()
        logger.info("Scraper thread started with 1-second updates")
        return True
    else:
        logger.info("Scraper is already running")
        return False

# Load existing data if available
def load_existing_data():
    try:
        # Load main data file
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                with scraper_state["lock"]:
                    scraper_state["data"] = data
                    scraper_state["last_updated"] = data.get("timestamp", datetime.now().isoformat())
                    logger.info(f"Loaded existing data with {len(data.get('matches', []))} matches")
        
        # Load ID mapping file
        if os.path.exists(ID_MAPPING_FILE):
            with open(ID_MAPPING_FILE, 'r', encoding='utf-8') as f:
                mapping_data = json.load(f)
                with scraper_state["lock"]:
                    scraper_state["id_mapping"] = mapping_data.get("mapping", {})
                    logger.info(f"Loaded ID mapping with {len(scraper_state['id_mapping'])} entries")
    except Exception as e:
        logger.error(f"Error loading existing data: {e}")

# Helper function to find matches by various IDs and handle redirects
def find_match_by_id(match_id: str):
    """Find a match by ID or in the ID mapping"""
    with scraper_state["lock"]:
        matches = scraper_state["data"].get("matches", [])
        id_mapping = scraper_state.get("id_mapping", {})
    
    # First, try direct lookup in current matches
    for match in matches:
        if match.get("id") == match_id:
            return match, None  # Found direct match
    
    # If not found directly, check if it's in the ID mapping
    if match_id in id_mapping:
        new_id = id_mapping[match_id]
        # Check if we can find the match with the new ID
        for match in matches:
            if match.get("id") == new_id:
                return match, new_id  # Found mapped match
    
    # Try to resolve by team name-based matching as last resort
    if match_id.startswith('match_'):
        # Extract any potential dates or teams from the old ID
        parts = match_id.split('_')
        if len(parts) > 2:
            # Try to find matching teams in current matches
            for match in matches:
                team1 = match.get('team1', '')
                team2 = match.get('team2', '')
                if not team1:
                    continue
                    
                if (team1 and team1.lower() in match_id.lower()) or \
                   (team2 and team2.lower() in match_id.lower()):
                    # Potential match found
                    return match, match.get('id')
    
    # Not found at all
    return None, None

# API Endpoints
# [Rest of API endpoints remain unchanged]

# On startup
@app.on_event("startup")
async def startup_event():
    # Load existing data
    load_existing_data()
    
    # Initialize scraper state
    scraper_state["start_time"] = datetime.now()
    scraper_state["last_file_write"] = datetime.now()  # Initialize file write timestamp
    
    # Start the scraper automatically
    start_scraper_thread()

# On shutdown
@app.on_event("shutdown")
async def shutdown_event():
    # Stop the scraper if running
    with scraper_state["lock"]:
        scraper_state["is_running"] = False
        logger.info("API shutting down, stopping scraper")

if __name__ == "__main__":
    # Use the PORT environment variable provided by Render
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
