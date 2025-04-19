import time
import random
import json
import pandas as pd
import os
import signal
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, BackgroundTasks, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import re

app = FastAPI(
    title="1xbet Odds API",
    description="API for scraping and monitoring sports betting odds from 1xbet.",
    version="1.0.0"
)

# Add CORS middleware to allow cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For production, specify allowed origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state for background task
continuous_task_running = False
last_scrape_time = None
task_status = "idle"

# In-memory storage for scraped data
data_store = {
    "live_events": [],
    "upcoming_events": [],
    "leagues": [],
    "odds_changes": []
}

# Pydantic models for API responses
class ScrapeStatus(BaseModel):
    status: str
    last_scrape: Optional[str] = None
    live_events_count: int = 0
    upcoming_events_count: int = 0
    leagues_count: int = 0

class XbetScraper:
    def __init__(self):
        self.base_url = "https://ind.1xbet.com/"
        self.update_interval = 3  # Update odds every 3 seconds
        self.running = True
        self.last_update = None
        
        # Setup Chrome options - optimized for Render environment
        self.chrome_options = Options()
        self.chrome_options.add_argument("--headless")
        self.chrome_options.add_argument("--no-sandbox")
        self.chrome_options.add_argument("--disable-dev-shm-usage")
        self.chrome_options.add_argument("--disable-gpu")
        self.chrome_options.add_argument("--window-size=1280,720")  # Reduced window size
        self.chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
        self.chrome_options.add_argument("--disable-extensions")
        self.chrome_options.add_argument("--disable-infobars")
        self.chrome_options.add_argument("--single-process")  # Use a single process to save memory
        self.chrome_options.add_argument("--blink-settings=imagesEnabled=false")  # Disable images to save memory
        
        # Memory optimized configurations
        self.chrome_options.add_argument("--js-flags=--expose-gc")
        self.chrome_options.add_argument("--disable-features=TranslateUI")
        self.chrome_options.add_argument("--disable-notifications")
        
        try:
            # Initialize WebDriver with ChromeDriverManager - handling Render environment
            print("Setting up Chrome WebDriver...")
            
            # Check if we're in a render environment and use appropriate setup
            if os.environ.get('RENDER'):
                # On Render, use the Chrome binary that was installed in start.sh
                chrome_binary_path = "/usr/bin/google-chrome"
                service = Service(ChromeDriverManager().install())
                self.driver = webdriver.Chrome(service=service, options=self.chrome_options)
            else:
                # For local development
                service = Service(ChromeDriverManager().install())
                self.driver = webdriver.Chrome(service=service, options=self.chrome_options)
                
            self.wait = WebDriverWait(self.driver, 10)
            print("WebDriver initialized successfully")
        except Exception as e:
            print(f"Error initializing WebDriver: {e}")
            sys.exit(1)
        
        # Data storage
        self.live_events = []
        self.upcoming_events = []
        self.leagues = []
        
        # Setup signal handler for clean termination
        signal.signal(signal.SIGINT, self.signal_handler)
    
    def signal_handler(self, sig, frame):
        """Handle Ctrl+C to exit cleanly"""
        print("\nReceived termination signal. Shutting down gracefully...")
        self.running = False
    
    def __del__(self):
        """Close the browser when done"""
        if hasattr(self, 'driver'):
            try:
                self.driver.quit()
                print("WebDriver closed successfully")
            except:
                print("Error closing WebDriver")
    
    def get_page_content(self, url=None):
        """Fetch the page content with Selenium and wait for it to load"""
        try:
            target_url = url if url else self.base_url
            print(f"Fetching page: {target_url}")
            self.driver.get(target_url)
            
            # Wait for the content to load
            try:
                self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".c-events__item")))
                print("Main content elements loaded")
            except:
                print("Warning: Timed out waiting for .c-events__item, will try to continue")
            
            # Enhanced scrolling to ensure all content is loaded - optimized for render
            print("Scrolling page to load more content...")
            # First scroll to middle
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight/3);")
            time.sleep(0.5)
            # Then scroll to two-thirds
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight*2/3);")
            time.sleep(0.5)
            # Finally scroll to bottom
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
            
            print("Page loaded successfully")
            return self.driver.page_source
        except Exception as e:
            print(f"Error fetching page: {e}")
            return None
    
    def parse_live_events(self, html_content):
        """Parse the live events section of the page"""
        soup = BeautifulSoup(html_content, 'html.parser')
        live_events = []
        
        # Find the container for live events - looking for the LIVE Bets section
        live_container = soup.select_one('div[id="line_bets_on_main"].c-events.greenBack')
        if not live_container:
            print("Live events container not found")
            return live_events
            
        # Find all live events containers - these are the league sections
        live_sections = live_container.select('.dashboard-champ-content')
        print(f"Found {len(live_sections)} live sections")
        
        for section_index, section in enumerate(live_sections):
            # Get league info from the header
            league_header = section.select_one('.c-events__item_head')
            if not league_header:
                print(f"No header found for section {section_index}")
                continue
                
            # Get sport type
            sport_icon = league_header.select_one('.icon use')
            sport_type = sport_icon['xlink:href'].split('#')[-1].replace('sports_', '') if sport_icon else "Unknown"
            sport_name = self.get_sport_name(sport_type)
            
            # Get country
            country_element = league_header.select_one('.flag-icon use')
            country = country_element['xlink:href'].split('#')[-1] if country_element else "International"
            
            # Get league name
            league_name_element = league_header.select_one('.c-events__liga')
            league_name = league_name_element.text.strip() if league_name_element else "Unknown League"
            league_url = league_name_element['href'] if league_name_element and 'href' in league_name_element.attrs else ""
            
            print(f"Processing league: {league_name} ({sport_name})")
            
            # Get the available bet types for this league
            bet_types = []
            bet_title_elements = league_header.select('.c-bets__title')
            for title_elem in bet_title_elements:
                bet_types.append(title_elem.text.strip())
            
            print(f"Available bet types: {bet_types}")
            
            # Get all matches in this league
            matches = section.select('.c-events__item_col .c-events__item_game')
            print(f"Found {len(matches)} matches in {league_name}")
            
            for match_index, match in enumerate(matches):
                match_data = {
                    'sport': sport_name,
                    'country': country,
                    'league': league_name,
                    'league_url': league_url,
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
                # Get team names
                teams_container = match.select_one('.c-events__teams')
                if teams_container:
                    team_elements = teams_container.select('.c-events__team')
                    if len(team_elements) >= 2:
                        match_data['team1'] = team_elements[0].text.strip()
                        match_data['team2'] = team_elements[1].text.strip()
                        print(f"Match {match_index+1}: {match_data['team1']} vs {match_data['team2']}")
                
                # Get match status and time
                time_element = match.select_one('.c-events__time')
                if time_element:
                    match_data['status'] = time_element.get_text(strip=True, separator=' ')
                
                # Get score - handling different score display formats
                score_cells = match.select('.c-events-scoreboard__cell--all')
                if score_cells:
                    scores = []
                    for score in score_cells:
                        if score.text.strip():
                            scores.append(score.text.strip())
                    
                    if scores:
                        match_data['scores'] = scores
                        match_data['score'] = ' - '.join(scores)
                
                # Create a unique ID for the match
                if 'team1' in match_data and 'team2' in match_data:
                    match_data['match_id'] = f"{sport_name}_{league_name}_{match_data['team1']}_{match_data['team2']}"
                else:
                    match_data['match_id'] = f"{sport_name}_{league_name}_{match_index}"
                
                # Get all odds for this match
                odds_cells = match.select('.c-bets__bet')
                for i, cell in enumerate(odds_cells):
                    if i < len(bet_types):
                        bet_type = bet_types[i]
                        # Look for the odds value
                        odds_value_elem = cell.select_one('.c-bets__inner')
                        if odds_value_elem and not 'non' in cell.get('class', []):
                            odds_value = odds_value_elem.text.strip()
                            match_data[f'odd_{bet_type}'] = odds_value
                
                # Get the match URL
                match_url_element = match.select_one('a.c-events__name')
                if match_url_element and 'href' in match_url_element.attrs:
                    match_data['match_url'] = match_url_element['href']
                
                # Capture any other important data
                # Some matches have additional information like yellow/red cards, etc.
                icons = match.select('.c-events__ico')
                if icons:
                    match_data['has_video'] = any('c-events__ico_video' in icon.get('class', []) for icon in icons)
                    match_data['has_statistics'] = any('c-events__ico--statistics' in icon.get('class', []) for icon in icons)
                
                live_events.append(match_data)
                
        print(f"Successfully parsed {len(live_events)} live events")
        return live_events
    
    def parse_upcoming_events(self, html_content):
        """Parse the upcoming (non-live) events section of the page"""
        soup = BeautifulSoup(html_content, 'html.parser')
        upcoming_events = []
        
        # Find the Sportsbook section (blueBack container)
        upcoming_container = soup.select_one('div[id="line_bets_on_main"].c-events.blueBack')
        if not upcoming_container:
            print("Upcoming events container not found")
            return upcoming_events
            
        # Find all upcoming events containers
        upcoming_sections = upcoming_container.select('.dashboard-champ-content')
        print(f"Found {len(upcoming_sections)} upcoming sections")
        
        for section_index, section in enumerate(upcoming_sections):
            # Get league info
            league_header = section.select_one('.c-events__item_head')
            if not league_header:
                print(f"No header found for section {section_index}")
                continue
                
            # Get sport type
            sport_icon = league_header.select_one('.icon use')
            sport_type = sport_icon['xlink:href'].split('#')[-1].replace('sports_', '') if sport_icon else "Unknown"
            sport_name = self.get_sport_name(sport_type)
            
            # Get country
            country_element = league_header.select_one('.flag-icon use')
            country = country_element['xlink:href'].split('#')[-1] if country_element else "International"
            
            # Get league name
            league_name_element = league_header.select_one('.c-events__liga')
            league_name = league_name_element.text.strip() if league_name_element else "Unknown League"
            league_url = league_name_element['href'] if league_name_element and 'href' in league_name_element.attrs else ""
            
            print(f"Processing league: {league_name} ({sport_name})")
            
            # Get the available bet types for this league
            bet_types = []
            bet_title_elements = league_header.select('.c-bets__title')
            for title_elem in bet_title_elements:
                bet_types.append(title_elem.text.strip())
            
            print(f"Available bet types: {bet_types}")
            
            # Track current date for all matches in this section
            current_date = None
            
            # Get all matches in this league
            match_items = section.select('.c-events__item_col')
            
            for item_index, item in enumerate(match_items):
                # Check if this is a date header
                date_element = item.select_one('.c-events__date')
                if date_element:
                    current_date = date_element.text.strip()
                    print(f"Found date: {current_date}")
                    continue
                
                # Get match element
                match = item.select_one('.c-events__item_game')
                if not match:
                    continue
                
                match_data = {
                    'sport': sport_name,
                    'country': country,
                    'league': league_name,
                    'league_url': league_url,
                    'match_date': current_date,
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
                # Get team names
                teams_container = match.select_one('.c-events__teams')
                if teams_container:
                    team_elements = teams_container.select('.c-events__team')
                    if len(team_elements) >= 2:
                        match_data['team1'] = team_elements[0].text.strip()
                        match_data['team2'] = team_elements[1].text.strip()
                        print(f"Match {item_index}: {match_data['team1']} vs {match_data['team2']}")
                
                # Get match time
                time_element = match.select_one('.c-events-time__val')
                if time_element:
                    match_data['start_time'] = time_element.text.strip()
                
                # Create a unique ID for the match
                if 'team1' in match_data and 'team2' in match_data:
                    match_data['match_id'] = f"{sport_name}_{league_name}_{match_data['team1']}_{match_data['team2']}"
                else:
                    match_data['match_id'] = f"{sport_name}_{league_name}_{item_index}"
                
                # Get all odds for this match
                odds_cells = match.select('.c-bets__bet')
                for i, cell in enumerate(odds_cells):
                    if i < len(bet_types):
                        bet_type = bet_types[i]
                        # Look for the odds value
                        odds_value_elem = cell.select_one('.c-bets__inner')
                        if odds_value_elem and not 'non' in cell.get('class', []):
                            odds_value = odds_value_elem.text.strip()
                            match_data[f'odd_{bet_type}'] = odds_value
                
                # Get the match URL
                match_url_element = match.select_one('a.c-events__name')
                if match_url_element and 'href' in match_url_element.attrs:
                    match_data['match_url'] = match_url_element['href']
                
                # Capture starting time info
                starts_in_element = match.select_one('div[title^="Starts in"]')
                if starts_in_element:
                    starts_in_text = starts_in_element.get('title', '')
                    match_data['starts_in'] = starts_in_text.replace('Starts in ', '')
                
                # Capture any statistics links
                stat_elements = match.select('.c-events-statistics__item')
                if stat_elements:
                    match_data['has_statistics'] = True
                    stat_types = []
                    for stat in stat_elements:
                        stat_title = stat.select_one('.c-events-statistics__title')
                        if stat_title:
                            stat_types.append(stat_title.text.strip())
                    if stat_types:
                        match_data['available_statistics'] = stat_types
                
                upcoming_events.append(match_data)
                
        print(f"Successfully parsed {len(upcoming_events)} upcoming events")
        return upcoming_events
    
    def get_sport_name(self, sport_id):
        """Convert sport ID to readable name"""
        sport_mapping = {
            '1': 'Football',
            '2': 'Ice Hockey',
            '3': 'Basketball',
            '4': 'Tennis',
            '10': 'Table Tennis',
            '66': 'Cricket',
            '85': 'FIFA',
            '95': 'Volleyball',
            '17': 'Hockey',
            '29': 'Baseball',
            '107': 'Darts',
            '128': 'Handball',
        }
        return sport_mapping.get(sport_id, f"Sport {sport_id}")
    
    def get_all_leagues(self, html_content=None):
        """Get a list of all available leagues on the homepage"""
        if not html_content:
            html_content = self.get_page_content()
            if not html_content:
                return []
        
        soup = BeautifulSoup(html_content, 'html.parser')
        leagues = []
        
        # Find all league headers across both live and upcoming sections
        league_headers = soup.select('.c-events__item_head')
        print(f"Found {len(league_headers)} league headers")
        
        for i, header in enumerate(league_headers):
            # Skip duplicate leagues
            league_element = header.select_one('.c-events__liga')
            if not league_element:
                continue
            
            # Get information about this league
            league_name = league_element.text.strip()
            league_url = league_element['href'] if 'href' in league_element.attrs else ""
            
            # Get sport type
            sport_icon = header.select_one('.icon use')
            sport_type = sport_icon['xlink:href'].split('#')[-1].replace('sports_', '') if sport_icon else "Unknown"
            sport_name = self.get_sport_name(sport_type)
            
            # Get country
            country_element = header.select_one('.flag-icon use')
            country = country_element['xlink:href'].split('#')[-1] if country_element else "International"
            
            # Create league data object
            league_data = {
                'name': league_name,
                'url': league_url,
                'sport': sport_name,
                'country': country,
                'league_id': f"{sport_name}_{league_name}"
            }
            
            # Check if league has a logo
            logo_element = header.select_one('.champ-logo__img')
            if logo_element and 'src' in logo_element.attrs:
                league_data['logo_url'] = logo_element['src']
                
            # Check if this is a top event
            is_top_section = header.find_parent('div', class_='top-champs-banner')
            if is_top_section:
                league_data['is_top_event'] = True
            
            # Avoid duplicates
            if not any(l['league_id'] == league_data['league_id'] for l in leagues):
                leagues.append(league_data)
                print(f"League {i+1}: {league_data['name']} ({league_data['sport']})")
        
        return leagues
    
    def update_match_odds(self, existing_match, new_match):
        """Update odds and any changed data in an existing match with new data"""
        # Track if odds have changed
        odds_changed = False
        
        # Update timestamp
        existing_match['timestamp'] = new_match['timestamp']
        
        # Update score if available
        if 'score' in new_match:
            if 'score' not in existing_match or existing_match['score'] != new_match['score']:
                print(f"Score updated for {existing_match.get('team1', '')} vs {existing_match.get('team2', '')}: {existing_match.get('score', 'No score')} → {new_match['score']}")
                existing_match['score'] = new_match['score']
                odds_changed = True
        
        # Update scores array if available
        if 'scores' in new_match:
            if 'scores' not in existing_match or existing_match['scores'] != new_match['scores']:
                existing_match['scores'] = new_match['scores']
        
        # Update match status if available
        if 'status' in new_match and ('status' not in existing_match or existing_match['status'] != new_match['status']):
            existing_match['status'] = new_match['status']
            odds_changed = True
        
        # Update all other fields
        for key, value in new_match.items():
            # Skip already handled fields and the match_id
            if key in ['timestamp', 'score', 'scores', 'status', 'match_id']:
                continue
                
            # Check if it's an odds field that has changed
            if key.startswith('odd_'):
                if key not in existing_match or existing_match[key] != value:
                    print(f"Odds updated for {existing_match.get('team1', '')} vs {existing_match.get('team2', '')}: {key} changed from {existing_match.get(key, 'N/A')} → {value}")
                    existing_match[key] = value
                    odds_changed = True
            # Update any other fields that might have changed
            elif key not in existing_match or existing_match[key] != value:
                existing_match[key] = value
        
        return odds_changed

    # API method - single execution
    def run_single_scrape(self):
        """Run a single scraping operation"""
        print("Starting single scrape...")
        
        html_content = self.get_page_content()
        if not html_content:
            raise Exception("Failed to retrieve the main page")
        
        # Parse live events
        print("Parsing live events...")
        live_events = self.parse_live_events(html_content)
        
        # Parse upcoming events
        print("Parsing upcoming events...")
        upcoming_events = self.parse_upcoming_events(html_content)
        
        # Get all leagues
        print("Getting all leagues...")
        leagues = self.get_all_leagues(html_content)
        
        print("Scraping completed successfully!")
        return {
            'live_events': live_events,
            'upcoming_events': upcoming_events,
            'leagues': leagues
        }
    
    def run_continuous_updates(self, interval=5, max_updates=None):
        """Run continuous updates of odds"""
        global continuous_task_running, last_scrape_time, task_status, data_store
        
        print(f"Starting continuous odds updates every {interval} seconds")
        continuous_task_running = True
        task_status = "running"
        odds_changes = []
        
        try:
            # Initialize with first fetch
            html_content = self.get_page_content()
            if not html_content:
                raise Exception("Failed to retrieve the main page")
            
            # Initial parsing
            live_events = self.parse_live_events(html_content)
            upcoming_events = self.parse_upcoming_events(html_content)
            leagues = self.get_all_leagues(html_content)
            
            # Store data
            data_store["live_events"] = live_events
            data_store["upcoming_events"] = upcoming_events
            data_store["leagues"] = leagues
            
            update_count = 0
            while continuous_task_running:
                if max_updates and update_count >= max_updates:
                    print(f"Reached maximum updates ({max_updates}). Stopping.")
                    break
                
                update_count += 1
                last_scrape_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n=== Update #{update_count} at {last_scrape_time} ===")
                
                # Preserve old data to check for changes
                old_live_events = data_store["live_events"].copy()
                old_upcoming_events = data_store["upcoming_events"].copy()
                
                # Wait for the next update
                time.sleep(interval)
                
                # Skip if task was stopped during sleep
                if not continuous_task_running:
                    break
                
                # Refresh page content
                html_content = self.get_page_content()
                if not html_content:
                    print("Failed to retrieve the page. Skipping this update.")
                    continue
                
                # Parse updated data
                new_live_events = self.parse_live_events(html_content)
                new_upcoming_events = self.parse_upcoming_events(html_content)
                
                # Create maps for quick lookup by match_id
                live_map = {match['match_id']: match for match in data_store["live_events"] if 'match_id' in match}
                upcoming_map = {match['match_id']: match for match in data_store["upcoming_events"] if 'match_id' in match}
                
                # Track which matches have changed
                changed_live_matches = []
                changed_upcoming_matches = []
                new_live_matches = []
                new_upcoming_matches = []
                
                # Update live events
                for new_match in new_live_events:
                    if 'match_id' not in new_match:
                        continue
                        
                    match_id = new_match['match_id']
                    if match_id in live_map:
                        if self.update_match_odds(live_map[match_id], new_match):
                            changed_live_matches.append(live_map[match_id])
                    else:
                        new_live_matches.append(new_match)
                        data_store["live_events"].append(new_match)
                
                # Update upcoming events
                for new_match in new_upcoming_events:
                    if 'match_id' not in new_match:
                        continue
                        
                    match_id = new_match['match_id']
                    if match_id in upcoming_map:
                        if self.update_match_odds(upcoming_map[match_id], new_match):
                            changed_upcoming_matches.append(upcoming_map[match_id])
                    else:
                        new_upcoming_matches.append(new_match)
                        data_store["upcoming_events"].append(new_match)
                
                # Log changes
                print(f"Live events: {len(data_store['live_events'])} total, {len(new_live_matches)} new, {len(changed_live_matches)} updated")
                print(f"Upcoming events: {len(data_store['upcoming_events'])} total, {len(new_upcoming_matches)} new, {len(changed_upcoming_matches)} updated")
                
                # Track odds changes
                if changed_live_matches or changed_upcoming_matches:
                    changes_data = {
                        'timestamp': last_scrape_time,
                        'live_changes': [],
                        'upcoming_changes': []
                    }
                    
                    for match in changed_live_matches:
                        old_match = next((m for m in old_live_events if 'match_id' in m and m['match_id'] == match['match_id']), {})
                        match_changes = {
                            'match_id': match['match_id'],
                            'team1': match.get('team1', ''),
                            'team2': match.get('team2', ''),
                            'changes': {}
                        }
                        for key in match:
                            if key.startswith('odd_') and key in old_match and old_match[key] != match[key]:
                                match_changes['changes'][key] = {
                                    'from': old_match[key],
                                    'to': match[key]
                                }
                        if match_changes['changes']:
                            changes_data['live_changes'].append(match_changes)
                    
                    for match in changed_upcoming_matches:
                        old_match = next((m for m in old_upcoming_events if 'match_id' in m and m['match_id'] == match['match_id']), {})
                        match_changes = {
                            'match_id': match['match_id'],
                            'team1': match.get('team1', ''),
                            'team2': match.get('team2', ''),
                            'changes': {}
                        }
                        for key in match:
                            if key.startswith('odd_') and key in old_match and old_match[key] != match[key]:
                                match_changes['changes'][key] = {
                                    'from': old_match[key],
                                    'to': match[key]
                                }
                        if match_changes['changes']:
                            changes_data['upcoming_changes'].append(match_changes)
                    
                    if changes_data['live_changes'] or changes_data['upcoming_changes']:
                        data_store["odds_changes"].append(changes_data)
                
                # Manage memory usage - keep only recent odds changes
                if len(data_store["odds_changes"]) > 100:
                    data_store["odds_changes"] = data_store["odds_changes"][-100:]
                
                # Every 10 updates, update the leagues as well
                if update_count % 10 == 0:
                    print("Updating leagues list...")
                    new_leagues = self.get_all_leagues(html_content)
                    if new_leagues:
                        data_store["leagues"] = new_leagues
                        
                # Force garbage collection to help with memory usage
                if update_count % 20 == 0:
                    import gc
                    gc.collect()
                    print("Forced garbage collection to manage memory")
        
        except Exception as e:
            print(f"Error in continuous scraping: {e}")
            task_status = "error"
        finally:
            task_status = "stopped"
            continuous_task_running = False
            print("Continuous scraping stopped")


# Function to create a scraper instance
def get_scraper():
    return XbetScraper()

# Background tasks
def run_continuous_scraper(interval=5, max_updates=None):
    """Background task for continuous scraping"""
    global task_status
    
    # Create a new scraper instance
    scraper = XbetScraper()
    
    try:
        task_status = "starting"
        scraper.run_continuous_updates(interval=interval, max_updates=max_updates)
    except Exception as e:
        task_status = "error"
        print(f"Error in continuous scraping task: {e}")
    finally:
        if scraper:
            try:
                scraper.__del__()
            except:
                pass

# API Endpoints
@app.get("/", tags=["Info"])
async def root():
    """API root - returns basic information"""
    return {
        "name": "1xbet Odds API",
        "version": "1.0.0",
        "description": "API for scraping sports betting odds from 1xbet",
        "documentation": "/docs"
    }

@app.get("/api/status", tags=["Monitoring"], response_model=ScrapeStatus)
async def get_status():
    """Get current status of the scraper"""
    global continuous_task_running, last_scrape_time, task_status, data_store
    
    return {
        "status": task_status,
        "last_scrape": last_scrape_time,
        "live_events_count": len(data_store["live_events"]),
        "upcoming_events_count": len(data_store["upcoming_events"]),
        "leagues_count": len(data_store["leagues"])
    }

@app.get("/api/scrape", tags=["Scraping"])
async def scrape_all():
    """Perform a one-time scrape of all data"""
    try:
        scraper = get_scraper()
        result = scraper.run_single_scrape()
        
        # Update the data store
        global data_store, last_scrape_time
        data_store["live_events"] = result["live_events"]
        data_store["upcoming_events"] = result["upcoming_events"]
        data_store["leagues"] = result["leagues"]
        last_scrape_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        return {
            "success": True,
            "timestamp": last_scrape_time,
            "counts": {
                "live_events": len(result["live_events"]),
                "upcoming_events": len(result["upcoming_events"]),
                "leagues": len(result["leagues"])
            },
            "message": "Successfully scraped all data"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if scraper:
            try:
                scraper.__del__()
            except:
                pass

@app.get("/api/live", tags=["Data"])
async def get_live_events(
    sport: Optional[str] = None,
    country: Optional[str] = None,
    league: Optional[str] = None
):
    """Get all live events with optional filtering"""
    global data_store
    
    if not data_store["live_events"]:
        # If no data available, try to scrape first
        try:
            await scrape_all()
        except:
            # If scraping fails, return empty result
            return {"events": [], "count": 0}
    
    events = data_store["live_events"]
    
    # Apply filters
    if sport:
        events = [e for e in events if e.get('sport', '').lower() == sport.lower()]
    if country:
        events = [e for e in events if e.get('country', '').lower() == country.lower()]
    if league:
        events = [e for e in events if e.get('league', '').lower() == league.lower()]
    
    return {
        "events": events,
        "count": len(events),
        "timestamp": last_scrape_time
    }

@app.get("/api/upcoming", tags=["Data"])
async def get_upcoming_events(
    sport: Optional[str] = None,
    country: Optional[str] = None,
    league: Optional[str] = None,
    date: Optional[str] = None
):
    """Get all upcoming events with optional filtering"""
    global data_store
    
    if not data_store["upcoming_events"]:
        # If no data available, try to scrape first
        try:
            await scrape_all()
        except:
            # If scraping fails, return empty result
            return {"events": [], "count": 0}
    
    events = data_store["upcoming_events"]
    
    # Apply filters
    if sport:
        events = [e for e in events if e.get('sport', '').lower() == sport.lower()]
    if country:
        events = [e for e in events if e.get('country', '').lower() == country.lower()]
    if league:
        events = [e for e in events if e.get('league', '').lower() == league.lower()]
    if date:
        events = [e for e in events if e.get('match_date', '') == date]
    
    return {
        "events": events,
        "count": len(events),
        "timestamp": last_scrape_time
    }

@app.get("/api/leagues", tags=["Data"])
async def get_leagues(
    sport: Optional[str] = None,
    country: Optional[str] = None,
    top_only: bool = False
):
    """Get all leagues with optional filtering"""
    global data_store
    
    if not data_store["leagues"]:
        # If no data available, try to scrape first
        try:
            await scrape_all()
        except:
            # If scraping fails, return empty result
            return {"leagues": [], "count": 0}
    
    leagues = data_store["leagues"]
    
    # Apply filters
    if sport:
        leagues = [l for l in leagues if l.get('sport', '').lower() == sport.lower()]
    if country:
        leagues = [l for l in leagues if l.get('country', '').lower() == country.lower()]
    if top_only:
        leagues = [l for l in leagues if l.get('is_top_event', False)]
    
    return {
        "leagues": leagues,
        "count": len(leagues),
        "timestamp": last_scrape_time
    }

@app.get("/api/odds-changes", tags=["Data"])
async def get_odds_changes(
    limit: int = 10
):
    """Get historical odds changes"""
    global data_store
    
    changes = data_store["odds_changes"]
    
    # Limit the number of changes returned
    if limit > 0 and limit < len(changes):
        changes = changes[-limit:]
    
    return {
        "changes": changes,
        "count": len(changes)
    }

@app.post("/api/start-continuous", tags=["Monitoring"])
async def start_continuous(
    background_tasks: BackgroundTasks,
    interval: int = Query(5, ge=3, le=60, description="Update interval in seconds (min 3, max 60)"),
    max_updates: Optional[int] = Query(None, ge=1, description="Maximum number of updates before stopping (optional)")
):
    """Start continuous scraping in the background"""
    global continuous_task_running, task_status
    
    if continuous_task_running:
        return {
            "success": False,
            "message": "Continuous scraping is already running"
        }
    
    # Reset the status
    task_status = "starting"
    
    # Start the background task
    background_tasks.add_task(run_continuous_scraper, interval, max_updates)
    
    return {
        "success": True,
        "message": f"Started continuous scraping with interval of {interval} seconds",
        "max_updates": max_updates if max_updates else "unlimited"
    }

@app.post("/api/stop-continuous", tags=["Monitoring"])
async def stop_continuous():
    """Stop continuous scraping"""
    global continuous_task_running, task_status
    
    if not continuous_task_running:
        return {
            "success": False,
            "message": "No continuous scraping is currently running"
        }
    
    continuous_task_running = False
    task_status = "stopping"
    
    return {
        "success": True,
        "message": "Stopping continuous scraping"
    }

@app.get("/api/match/{match_id}", tags=["Data"])
async def get_match_by_id(match_id: str):
    """Get detailed information about a specific match by ID"""
    global data_store
    
    # Search in both live and upcoming events
    all_events = data_store["live_events"] + data_store["upcoming_events"]
    
    # Find the match
    match = next((m for m in all_events if m.get('match_id') == match_id), None)
    
    if not match:
        raise HTTPException(status_code=404, detail=f"Match with ID {match_id} not found")
    
    return {
        "match": match,
        "timestamp": last_scrape_time
    }

@app.get("/api/sports", tags=["Data"])
async def get_sports():
    """Get list of all available sports"""
    global data_store
    
    if not data_store["leagues"]:
        # If no data available, try to scrape first
        try:
            await scrape_all()
        except:
            # If scraping fails, return empty result
            return {"sports": [], "count": 0}
    
    # Extract unique sports from leagues
    sports = list({l['sport'] for l in data_store["leagues"] if 'sport' in l})
    sports.sort()
    
    return {
        "sports": sports,
        "count": len(sports)
    }

@app.get("/api/countries", tags=["Data"])
async def get_countries(sport: Optional[str] = None):
    """Get list of all available countries, optionally filtered by sport"""
    global data_store
    
    if not data_store["leagues"]:
        # If no data available, try to scrape first
        try:
            await scrape_all()
        except:
            # If scraping fails, return empty result
            return {"countries": [], "count": 0}
    
    leagues = data_store["leagues"]
    
    # Filter by sport if specified
    if sport:
        leagues = [l for l in leagues if l.get('sport', '').lower() == sport.lower()]
    
    # Extract unique countries
    countries = list({l['country'] for l in leagues if 'country' in l})
    countries.sort()
    
    return {
        "countries": countries,
        "count": len(countries)
    }

# Health check endpoint for Render
@app.get("/health", tags=["Monitoring"])
async def health_check():
    """Health check endpoint for Render"""
    return {"status": "ok"}

# This is the app instance that Gunicorn will use
app = app  # This line makes the 'app' variable explicitly available at module level

# Run the API server if executed directly (for local development)
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
