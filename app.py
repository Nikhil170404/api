#!/usr/bin/env python3
"""
Cricket Odds API for 1xbet

This API serves cricket odds data scraped from ind.1xbet.com
and provides endpoints for accessing the data with stable IDs.
"""

import os
import re
import time
import json
import logging
import threading
import uvicorn
import asyncio
import sys
import subprocess
import random
from typing import List, Dict, Any, Optional, Union
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, status, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

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

# Initialize FastAPI app
app = FastAPI(
    title="Cricket Odds API",
    description="API for real-time cricket odds from 1xbet",
    version="3.0.0",
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
    "lock": threading.Lock()
}

# Try to install and import relevant packages first
try:
    # Check if we need to install dependencies
    if IS_PRODUCTION:
        logger.info("Installing required packages in production environment")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "webdriver-manager", "selenium", "playwright"])
        # Try to install playwright browsers
        try:
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
            logger.info("Playwright browsers installed successfully")
        except Exception as e:
            logger.warning(f"Failed to install Playwright browsers: {e}")
    
    # Import the required packages
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
    
    # Try to import webdriver_manager
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        from webdriver_manager.core.utils import ChromeType
        WEBDRIVER_MANAGER_AVAILABLE = True
        logger.info("WebDriver Manager is available")
    except ImportError:
        WEBDRIVER_MANAGER_AVAILABLE = False
        logger.warning("WebDriver Manager not available, will use system ChromeDriver")
    
    # Try to import playwright
    try:
        from playwright.sync_api import sync_playwright
        PLAYWRIGHT_AVAILABLE = True
        logger.info("Playwright is available")
    except ImportError:
        PLAYWRIGHT_AVAILABLE = False
        logger.warning("Playwright not available, will use Selenium only")
    
except Exception as e:
    logger.error(f"Error importing browser automation libraries: {e}")
    # We'll need to handle this gracefully in the application

class CricketOddsScraper:
    """Scraper for extracting cricket odds from 1xbet"""
    
    def __init__(self, url="https://ind.1xbet.com/live/cricket"):
        self.url = url
        self.driver = None
        self.retry_count = 0
        self.max_retries = 5
        self.error_count = 0
        self.max_continuous_errors = 10
        self.force_refresh = False
        self.use_playwright = False
        self.playwright = None
        self.browser = None
        self.page = None
        self.navigation_timeout = int(os.environ.get('SELENIUM_TIMEOUT', 120))
        
        # Set of user agents to rotate
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0"
        ]
        
        # Try to import fake-useragent for better randomization
        try:
            from fake_useragent import UserAgent
            self.ua = UserAgent()
            self.has_fake_ua = True
        except ImportError:
            self.has_fake_ua = False
    
    def setup_driver(self):
        """Set up the browser driver with fallback options"""
        # Check if we should use Playwright
        if self.use_playwright and PLAYWRIGHT_AVAILABLE:
            return self._setup_playwright()
        
        # Use Selenium as the default option
        try:
            # Close existing driver if any
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
            
            # Get a random user agent
            if self.has_fake_ua:
                user_agent = self.ua.random
            else:
                user_agent = random.choice(self.user_agents)
                
            logger.info(f"Using user agent: {user_agent}")
            
            # Configure Chrome options
            chrome_options = Options()
            chrome_options.add_argument("--headless=new")  # Use new headless mode
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
            
            # Add options to bypass detection
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option("useAutomationExtension", False)
            
            # Add language and geolocation
            chrome_options.add_argument("--lang=en-US,en;q=0.9")
            chrome_options.add_argument("--accept-lang=en-US,en;q=0.9")
            
            # Add user agent
            chrome_options.add_argument(f"user-agent={user_agent}")
            
            # Try with WebDriver Manager if available
            if WEBDRIVER_MANAGER_AVAILABLE:
                try:
                    logger.info("Setting up Chrome with webdriver-manager")
                    self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
                    logger.info("Successfully created WebDriver with webdriver-manager")
                    self.retry_count = 0
                    
                    # Execute CDP commands to disable automation flags
                    self._disable_automation_flags()
                    return True
                except Exception as e:
                    logger.error(f"Webdriver-manager setup failed: {e}")
            
            # Try with system Chrome
            try:
                logger.info("Trying with system Chrome")
                self.driver = webdriver.Chrome(options=chrome_options)
                logger.info("Successfully created WebDriver with system Chrome")
                self.retry_count = 0
                
                # Execute CDP commands to disable automation flags
                self._disable_automation_flags()
                return True
            except Exception as e:
                logger.error(f"System Chrome attempt failed: {e}")
            
            # Fall back to Playwright if all Selenium attempts failed
            if PLAYWRIGHT_AVAILABLE:
                logger.info("Falling back to Playwright")
                self.use_playwright = True
                return self._setup_playwright()
            
            # All attempts failed
            self.retry_count += 1
            self.error_count += 1
            if self.retry_count < self.max_retries:
                logger.info(f"Retrying driver setup (attempt {self.retry_count}/{self.max_retries})...")
                time.sleep(5)
                return self.setup_driver()
            
            return False
        except Exception as e:
            logger.error(f"Error initializing WebDriver: {e}")
            self.retry_count += 1
            self.error_count += 1
            
            # Try Playwright as fallback
            if PLAYWRIGHT_AVAILABLE and not self.use_playwright:
                logger.info("Trying Playwright after Selenium error")
                self.use_playwright = True
                return self._setup_playwright()
            
            if self.retry_count < self.max_retries:
                logger.info(f"Retrying driver setup (attempt {self.retry_count}/{self.max_retries})...")
                time.sleep(5)
                return self.setup_driver()
            
            return False
            
    def _disable_automation_flags(self):
        """Disable automation flags in Chrome for better stealth"""
        if not self.driver:
            return
            
        try:
            # Execute CDP commands to disable automation flags
            self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': '''
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                
                // Overwrite the `plugins` property to use a custom getter.
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        // This just needs to have `length > 0`, but we could mock the plugins too
                        return [1, 2, 3, 4, 5];
                    },
                });
                
                // Spoof languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });
                
                // Spoof user agent
                window.chrome = {
                    runtime: {},
                };
                
                // Pass the Permissions Test
                Object.defineProperty(navigator, 'permissions', {
                    get: () => ({
                        query: () => Promise.resolve({ state: 'granted' }),
                    }),
                });
                
                // Spoof platform
                Object.defineProperty(navigator, 'platform', {
                    get: () => 'Win32',
                });
                
                // Spoof hardwareConcurrency
                Object.defineProperty(navigator, 'hardwareConcurrency', {
                    get: () => 8,
                });
                
                // Spoof deviceMemory
                Object.defineProperty(navigator, 'deviceMemory', {
                    get: () => 8,
                });
                '''
            })
            
            # Apply additional CDP commands to spoof geo coordinates
            self.driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
                "latitude": 28.6139,  # Delhi, India coordinates 
                "longitude": 77.2090,
                "accuracy": 100
            })
            
        except Exception as e:
            logger.warning(f"Failed to disable automation flags: {e}")
            
    def _setup_playwright(self):
        """Set up Playwright as an alternative to Selenium"""
        if not PLAYWRIGHT_AVAILABLE:
            logger.error("Playwright not available")
            return False
        
        logger.info("Setting up Playwright")
        try:
            # Clean up existing browser if any
            if self.browser:
                try:
                    self.browser.close()
                except:
                    pass
                
            if self.playwright:
                try:
                    self.playwright.stop()
                except:
                    pass
            
            # Get a random user agent
            if self.has_fake_ua:
                user_agent = self.ua.random
            else:
                user_agent = random.choice(self.user_agents)
                
            logger.info(f"Using Playwright user agent: {user_agent}")
            
            # Start new playwright instance
            self.playwright = sync_playwright().start()
            
            # Create a browser context with specific options for stealth
            self.browser = self.playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-site-isolation-trials",
                ]
            )
            
            # Create a context with specific options
            context = self.browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="Asia/Kolkata",  # Set to India timezone
                geolocation={"latitude": 28.6139, "longitude": 77.2090},  # Delhi, India
                permissions=["geolocation"],
                java_script_enabled=True,
                bypass_csp=True,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "sec-ch-ua": '"Chromium";v="123", "Google Chrome";v="123", "Not;A=Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1"
                }
            )
            
            # Create a new page from the context
            self.page = context.new_page()
            
            # Add script to page to evade detection
            self.page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                
                window.chrome = {
                    runtime: {},
                };
                
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [
                        {
                            0: {type: "application/x-google-chrome-pdf", suffixes: "pdf", description: "Portable Document Format", enabledPlugin: true},
                            description: "Portable Document Format",
                            filename: "internal-pdf-viewer",
                            length: 1,
                            name: "Chrome PDF Plugin"
                        },
                        {
                            0: {type: "application/pdf", suffixes: "pdf", description: "Portable Document Format", enabledPlugin: true},
                            description: "Portable Document Format",
                            filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai",
                            length: 1,
                            name: "Chrome PDF Viewer"
                        },
                        {
                            0: {type: "application/x-nacl", suffixes: "", description: "Native Client Executable", enabledPlugin: true},
                            1: {type: "application/x-pnacl", suffixes: "", description: "Portable Native Client Executable", enabledPlugin: true},
                            description: "Native Client",
                            filename: "internal-nacl-plugin",
                            length: 2,
                            name: "Native Client"
                        }
                    ]
                });
            """)
            
            # Set flag to use Playwright for all subsequent operations
            self.use_playwright = True
            logger.info("Successfully set up Playwright")
            self.retry_count = 0
            return True
        except Exception as e:
            logger.error(f"Failed to set up Playwright: {e}")
            self.use_playwright = False
            self.retry_count += 1
            self.error_count += 1
            
            if self.retry_count < self.max_retries:
                logger.info(f"Retrying Playwright setup (attempt {self.retry_count}/{self.max_retries})...")
                time.sleep(5)
                return self._setup_playwright()
            
            return False
    
    def navigate_to_site(self):
        """Navigate to the website and wait for it to load"""
        if self.use_playwright:
            return self._navigate_with_playwright()
        else:
            return self._navigate_with_selenium()
    
    def _navigate_with_selenium(self):
        """Navigate using Selenium WebDriver"""
        try:
            # Add stealth mode behaviors first
            self._disable_automation_flags()
            
            logger.info(f"Navigating to {self.url} with Selenium")
            
            # First try to visit the main page to set cookies
            try:
                logger.info("First visiting main page to set cookies")
                self.driver.get("https://ind.1xbet.com/")
                time.sleep(5)  # Give some time for cookies to be set
            except Exception as e:
                logger.warning(f"Failed to visit main page first: {e}")
            
            # Navigate to site with extended timeout
            self.driver.set_page_load_timeout(self.navigation_timeout)
            self.driver.get(self.url)
            
            # Wait for the cricket section to load
            try:
                WebDriverWait(self.driver, self.navigation_timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".dashboard-champ-content"))
                )
                logger.info("Found .dashboard-champ-content with Selenium")
                return True
            except TimeoutException:
                # Try different selectors
                try:
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".c-events__item_head"))
                    )
                    logger.info("Found .c-events__item_head with Selenium")
                    return True
                except TimeoutException:
                    try:
                        WebDriverWait(self.driver, 5).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, ".c-events__team"))
                        )
                        logger.info("Found .c-events__team with Selenium")
                        return True
                    except TimeoutException:
                        pass
                
                # Check if we're being blocked (might redirect to captcha)
                page_text = self.driver.page_source.lower()
                if "captcha" in page_text or "robot" in page_text or "human" in page_text:
                    logger.error("Possible captcha or bot detection detected")
                elif "1xbet" not in page_text:
                    logger.error("Page doesn't contain expected 1xbet content")
                else:
                    logger.info("Page contains 1xbet content but not cricket section selectors")
                    # Just try to return true and proceed if we at least got to 1xbet
                    return True
                    
                # Debug: Save HTML
                try:
                    os.makedirs("debug_html", exist_ok=True)
                    with open("debug_html/blocked_page.html", "w", encoding="utf-8") as f:
                        f.write(self.driver.page_source)
                    logger.info("Saved blocked page HTML for debugging")
                    
                    # Also save a screenshot
                    self.driver.save_screenshot("debug_html/blocked_page.png")
                    logger.info("Saved screenshot for debugging")
                except Exception as e:
                    logger.error(f"Failed to save debug HTML: {e}")
                
                # Fall back to Playwright if available
                if PLAYWRIGHT_AVAILABLE and not self.use_playwright:
                    logger.info("Trying Playwright after possible blocking")
                    self.use_playwright = True
                    if self._setup_playwright():
                        return self._navigate_with_playwright()
                
                return False
        except WebDriverException as e:
            logger.error(f"WebDriver error while navigating: {e}")
            self.error_count += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error while navigating with Selenium: {e}")
            self.error_count += 1
            return False
    
    def _navigate_with_playwright(self):
        """Navigate using Playwright"""
        try:
            # Navigate with Playwright
            logger.info(f"Navigating to {self.url} with Playwright")
            
            # First try to visit the main page to set cookies
            try:
                logger.info("First visiting main page to set cookies with Playwright")
                self.page.goto("https://ind.1xbet.com/", wait_until="domcontentloaded")
                self.page.wait_for_timeout(5000)  # Wait 5 seconds for cookies to be set
            except Exception as e:
                logger.warning(f"Failed to visit main page first with Playwright: {e}")
            
            # Set longer timeout for navigation
            playwright_timeout = int(os.environ.get('PLAYWRIGHT_TIMEOUT', 120)) * 1000  # Convert to ms
            
            # Go to the cricket URL
            self.page.goto(self.url, timeout=playwright_timeout, wait_until="domcontentloaded")
            
            # Wait for the content to load
            self.page.wait_for_timeout(5000)  # Wait 5 seconds for JS to render
            
            # Try multiple selectors for cricket content
            for selector in [".dashboard-champ-content", ".c-events__item_head", ".c-events__team"]:
                try:
                    element = self.page.wait_for_selector(selector, timeout=10000)
                    if element:
                        logger.info(f"Found element with selector: {selector}")
                        return True
                except Exception as wait_error:
                    logger.warning(f"Could not find {selector}: {wait_error}")
            
            # Check if page was loaded at all
            page_text = self.page.content().lower()
            if "1xbet" in page_text:
                logger.info("Page loaded with 1xbet content, but selectors not found")
                
                # Save for debugging
                try:
                    os.makedirs("debug_html", exist_ok=True)
                    self.page.screenshot(path="debug_html/playwright_screenshot.png")
                    logger.info("Saved Playwright screenshot for debugging")
                    
                    with open("debug_html/playwright_content.html", "w", encoding="utf-8") as f:
                        f.write(self.page.content())
                    logger.info("Saved Playwright page content for debugging")
                except Exception as e:
                    logger.error(f"Failed to save debug information: {e}")
                
                # Try direct extraction even if selectors weren't found
                return True
            else:
                logger.error("Page doesn't contain 1xbet content with Playwright")
                
                # Possible blocking, capture screenshot
                try:
                    os.makedirs("debug_html", exist_ok=True)
                    self.page.screenshot(path="debug_html/playwright_blocked.png")
                    logger.info("Saved blocked screenshot for debugging")
                    
                    with open("debug_html/playwright_blocked.html", "w", encoding="utf-8") as f:
                        f.write(self.page.content())
                    logger.info("Saved blocked page content for debugging")
                except Exception as e:
                    logger.error(f"Failed to save debug information: {e}")
                    
                logger.error("Navigation failed, possibly blocked")
                self.error_count += 1
                return False
        except Exception as e:
            logger.error(f"Error navigating with Playwright: {e}")
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
        """Extract cricket odds data from the loaded page"""
        if self.use_playwright:
            return self._extract_with_playwright()
        else:
            return self._extract_with_selenium()
    
    def _extract_with_selenium(self):
        """Extract cricket odds data using Selenium"""
        matches = []
        
        try:
            # First try to find any content at all to help debug
            page_source = self.driver.page_source
            if "dashboard-champ-content" not in page_source:
                logger.warning("No dashboard-champ-content found in page source")
                if "c-events__item" not in page_source:
                    logger.warning("No c-events__item found in page source")
                    if "c-events__team" not in page_source:
                        logger.error("No cricket content identifiers found in page")
                        
                        # Save debug info
                        try:
                            os.makedirs("debug_html", exist_ok=True)
                            with open("debug_html/page_source.html", "w", encoding="utf-8") as f:
                                f.write(page_source)
                            self.driver.save_screenshot("debug_html/page_screenshot.png")
                        except Exception as e:
                            logger.error(f"Failed to save debug info: {e}")
                        
                        # Try to get from raw HTML if selectors aren't found
                        return self._extract_from_raw_html(page_source)
            
            # Try to find all cricket sections
            cricket_sections = self.driver.find_elements(By.CSS_SELECTOR, 'div.dashboard-champ-content')
            
            if not cricket_sections:
                logger.warning("No cricket sections found with dashboard-champ-content selector")
                
                # Try an alternative approach - look directly for match rows
                match_rows = self.driver.find_elements(By.CSS_SELECTOR, '.c-events__item_col')
                if match_rows:
                    logger.info(f"Found {len(match_rows)} match rows directly")
                    matches = self._process_match_rows(match_rows)
                    if matches:
                        return matches
                
                # Try to find any content to see if the page loaded at all
                any_content = self.driver.find_elements(By.CSS_SELECTOR, '*')
                logger.info(f"Total elements found on page: {len(any_content)}")
                
                # Save current page HTML for debugging
                try:
                    os.makedirs("debug_html", exist_ok=True)
                    with open("debug_html/no_cricket_sections.html", "w", encoding="utf-8") as f:
                        f.write(self.driver.page_source)
                    logger.info("Saved page HTML for debugging")
                except Exception as e:
                    logger.error(f"Failed to save debug HTML: {e}")
                
                # Try to extract from raw HTML
                return self._extract_from_raw_html(self.driver.page_source)
            
            logger.info(f"Found {len(cricket_sections)} potential cricket sections")
            
            for section in cricket_sections:
                try:
                    # Check league headers to find cricket leagues
                    header_elements = section.find_elements(By.CSS_SELECTOR, '.c-events__item_head')
                    
                    is_cricket_section = False
                    for elem in header_elements:
                        try:
                            # Look for the cricket icon in the header
                            if elem.find_elements(By.CSS_SELECTOR, 'svg.icon use[xlink:href*="sports_66"]'):
                                is_cricket_section = True
                                break
                        except (StaleElementReferenceException, NoSuchElementException):
                            continue
                    
                    if not is_cricket_section:
                        continue
                    
                    # Get league name for reference
                    league_name = ""
                    try:
                        league_elem = section.find_element(By.CSS_SELECTOR, '.c-events__liga')
                        league_name = league_elem.text.strip()
                    except NoSuchElementException:
                        pass
                        
                    # Get all match items in this section
                    match_items = section.find_elements(By.CSS_SELECTOR, '.c-events__item_col')
                    
                    section_matches = self._process_match_rows(match_items, league_name)
                    matches.extend(section_matches)
                        
                except (StaleElementReferenceException, NoSuchElementException) as e:
                    logger.warning(f"Error processing cricket section: {e}")
            
            if matches:
                logger.info(f"Extracted {len(matches)} cricket matches with Selenium")
                # Reset error count on successful extraction
                self.error_count = 0
            else:
                logger.warning("No cricket matches found with Selenium")
                self.error_count += 1
            
            return matches
            
        except Exception as e:
            logger.error(f"Error extracting cricket odds with Selenium: {e}")
            self.error_count += 1
            return []
            
    def _process_match_rows(self, match_items, league_name=""):
        """Process match rows to extract data"""
        matches = []
        
        for item in match_items:
            try:
                # Extract team names
                team1 = ""
                team2 = ""
                try:
                    team_elems = item.find_elements(By.CSS_SELECTOR, '.c-events__team')
                    if len(team_elems) >= 1:
                        team1 = team_elems[0].text.strip()
                        if len(team_elems) > 1:
                            team2 = team_elems[1].text.strip()
                except (StaleElementReferenceException, NoSuchElementException) as e:
                    logger.warning(f"Error extracting team names: {e}")

                # Skip if no team names found
                if not team1 and not team2:
                    continue

                # Create a stable ID based on team names
                stable_id = self._create_stable_id(team1, team2)
                
                # Initialize match data with stable ID
                match_data = {
                    'id': f"match_{stable_id}",
                    'timestamp': datetime.now().isoformat(),
                    'team1': team1,
                    'team2': team2,
                    'league': league_name  # Add league name for reference
                }
                
                # Extract additional match info
                try:
                    additional_info = item.find_elements(By.CSS_SELECTOR, '.c-events-scoreboard__additional-info')
                    if additional_info:
                        match_data['info'] = additional_info[0].text.strip()
                except (StaleElementReferenceException, NoSuchElementException):
                    pass
                
                # Extract current scores
                try:
                    score_cells = item.find_elements(By.CSS_SELECTOR, '.c-events-scoreboard__cell--all')
                    if score_cells and len(score_cells) > 0:
                        scores = [cell.text.strip() for cell in score_cells if cell.text.strip()]
                        if scores:
                            match_data['score'] = scores
                            match_data['in_play'] = True
                except (StaleElementReferenceException, NoSuchElementException) as e:
                    logger.warning(f"Error extracting score: {e}")
                    match_data['in_play'] = False
                
                # Extract odds
                odds = {'back': [], 'lay': []}
                
                try:
                    # Get all bet cells
                    bet_cells = item.find_elements(By.CSS_SELECTOR, '.c-bets__bet')
                    
                    # Process team 1 (back) odds - typically position 0 or 3
                    team1_odds_positions = [0, 3]  # Common positions for team1 odds
                    for pos in team1_odds_positions:
                        if pos < len(bet_cells):
                            cell = bet_cells[pos]
                            if "non" not in cell.get_attribute("class") and "c-bets__bet_coef" in cell.get_attribute("class"):
                                price_elem = cell.find_element(By.CSS_SELECTOR, '.c-bets__inner')
                                price = price_elem.text.strip()
                                if price and price != '-':
                                    odds['back'].append({
                                        'position': 0,
                                        'price': price,
                                        'volume': None
                                    })
                    
                    # Process team 2 (lay) odds - typically position 2 or 5
                    team2_odds_positions = [2, 5]  # Common positions for team2 odds
                    for pos in team2_odds_positions:
                        if pos < len(bet_cells):
                            cell = bet_cells[pos]
                            if "non" not in cell.get_attribute("class") and "c-bets__bet_coef" in cell.get_attribute("class"):
                                price_elem = cell.find_element(By.CSS_SELECTOR, '.c-bets__inner')
                                price = price_elem.text.strip()
                                if price and price != '-':
                                    odds['lay'].append({
                                        'position': 0,
                                        'price': price,
                                        'volume': None
                                    })
                    
                    # Process draw odds if available - typically position 1 or 4
                    draw_odds_positions = [1, 4]  # Common positions for draw odds
                    for pos in draw_odds_positions:
                        if pos < len(bet_cells):
                            cell = bet_cells[pos]
                            if "non" not in cell.get_attribute("class") and "c-bets__bet_coef" in cell.get_attribute("class"):
                                price_elem = cell.find_element(By.CSS_SELECTOR, '.c-bets__inner')
                                price = price_elem.text.strip()
                                if price and price != '-':
                                    # Add draw odds to a separate key
                                    match_data['draw_odds'] = price
                except (StaleElementReferenceException, NoSuchElementException) as e:
                    logger.warning(f"Error extracting odds: {e}")
                
                match_data['odds'] = odds
                matches.append(match_data)
            except (StaleElementReferenceException, NoSuchElementException) as e:
                logger.warning(f"Error processing match item: {e}")
                
        return matches
        
    def _extract_from_raw_html(self, html_content):
        """Extract match data from raw HTML when selectors fail"""
        import re
        matches = []
        
        # Extract teams using regex pattern
        team_pattern = r'<div class="c-events__team">\s*([^<]+?)\s*</div>'
        teams = re.findall(team_pattern, html_content)
        
        # Extract scores
        score_pattern = r'<span class="c-events-scoreboard__cell c-events-scoreboard__cell--all">([^<]+?)</span>'
        scores = re.findall(score_pattern, html_content)
        
        # Extract odds
        odds_pattern = r'<span class="c-bets__bet c-bets__bet_coef[^"]*"><span class="c-bets__inner">([^<]+?)</span></span>'
        odds_values = re.findall(odds_pattern, html_content)
        
        # Extract leagues
        league_pattern = r'class="c-events__liga"[^>]*>\s*([^<]+?)\s*</a>'
        leagues = re.findall(league_pattern, html_content)
        
        # If we found teams, create matches
        if teams and len(teams) >= 2:
            logger.info(f"Extracted {len(teams)} teams using regex")
            
            # Process in pairs for team1 and team2
            for i in range(0, len(teams), 2):
                if i + 1 < len(teams):
                    team1 = teams[i].strip()
                    team2 = teams[i+1].strip()
                    
                    # Create a stable ID
                    stable_id = self._create_stable_id(team1, team2)
                    
                    # Initialize match data
                    match_data = {
                        'id': f"match_{stable_id}",
                        'timestamp': datetime.now().isoformat(),
                        'team1': team1,
                        'team2': team2,
                        'league': leagues[i//2] if i//2 < len(leagues) else ""
                    }
                    
                    # Add scores if available
                    score_idx = i // 2 * 2
                    if score_idx + 1 < len(scores):
                        match_data['score'] = [scores[score_idx], scores[score_idx+1]]
                        match_data['in_play'] = True
                    
                    # Add odds if available
                    odds_idx = i // 2 * 2
                    odds = {'back': [], 'lay': []}
                    if odds_idx < len(odds_values):
                        odds['back'].append({
                            'position': 0,
                            'price': odds_values[odds_idx],
                            'volume': None
                        })
                    
                    if odds_idx + 1 < len(odds_values):
                        odds['lay'].append({
                            'position': 0,
                            'price': odds_values[odds_idx+1],
                            'volume': None
                        })
                    
                    match_data['odds'] = odds
                    matches.append(match_data)
        
        logger.info(f"Extracted {len(matches)} matches from raw HTML")
        return matches
    
    def _extract_with_playwright(self):
        """Extract cricket odds data using Playwright"""
        matches = []
        
        try:
            # Find all cricket sections
            cricket_sections = self.page.query_selector_all('div.dashboard-champ-content')
            
            if not cricket_sections:
                logger.warning("No cricket sections found with Playwright")
                # Save screenshot and page content for debugging
                try:
                    self.page.screenshot(path="debug_html/no_cricket_sections_pw.png")
                    with open("debug_html/no_cricket_sections_pw.html", "w", encoding="utf-8") as f:
                        f.write(self.page.content())
                    logger.info("Saved debug information for Playwright")
                except Exception as e:
                    logger.error(f"Failed to save debug information: {e}")
                return []
            
            logger.info(f"Found {len(cricket_sections)} potential cricket sections with Playwright")
            
            for section in cricket_sections:
                try:
                    # Check league headers to find cricket leagues
                    header_elements = section.query_selector_all('.c-events__item_head')
                    
                    is_cricket_section = False
                    for elem in header_elements:
                        # Look for the cricket icon in the header
                        cricket_icon = elem.query_selector('svg.icon use[xlink:href*="sports_66"]')
                        if cricket_icon:
                            is_cricket_section = True
                            break
                    
                    if not is_cricket_section:
                        continue
                    
                    # Get league name for reference
                    league_name = ""
                    league_elem = section.query_selector('.c-events__liga')
                    if league_elem:
                        league_name = league_elem.inner_text().strip()
                        
                    # Get all match items in this section
                    match_items = section.query_selector_all('.c-events__item_col')
                    
                    for item in match_items:
                        # Extract team names
                        team1 = ""
                        team2 = ""
                        team_elems = item.query_selector_all('.c-events__team')
                        if len(team_elems) >= 1:
                            team1 = team_elems[0].inner_text().strip()
                            if len(team_elems) > 1:
                                team2 = team_elems[1].inner_text().strip()

                        # Create a stable ID based on team names
                        stable_id = self._create_stable_id(team1, team2)
                        
                        # Initialize match data with stable ID
                        match_data = {
                            'id': f"match_{stable_id}",
                            'timestamp': datetime.now().isoformat(),
                            'team1': team1,
                            'team2': team2,
                            'league': league_name  # Add league name for reference
                        }
                        
                        # Extract additional match info
                        additional_info = item.query_selector('.c-events-scoreboard__additional-info')
                        if additional_info:
                            match_data['info'] = additional_info.inner_text().strip()
                        
                        # Extract current scores
                        score_cells = item.query_selector_all('.c-events-scoreboard__cell--all')
                        if score_cells and len(score_cells) > 0:
                            scores = [cell.inner_text().strip() for cell in score_cells if cell.inner_text().strip()]
                            if scores:
                                match_data['score'] = scores
                                match_data['in_play'] = True
                        else:
                            match_data['in_play'] = False
                        
                        # Extract odds
                        odds = {'back': [], 'lay': []}
                        
                        # Get all bet cells
                        bet_cells = item.query_selector_all('.c-bets__bet')
                        
                        # Process team 1 (back) odds - typically position 0 or 3
                        team1_odds_positions = [0, 3]  # Common positions for team1 odds
                        for pos in team1_odds_positions:
                            if pos < len(bet_cells):
                                cell = bet_cells[pos]
                                class_attr = cell.get_attribute('class') or ""
                                if "non" not in class_attr:
                                    price_elem = cell.query_selector('.c-bets__inner')
                                    if price_elem:
                                        price = price_elem.inner_text().strip()
                                        if price and price != '-':
                                            odds['back'].append({
                                                'position': 0,
                                                'price': price,
                                                'volume': None
                                            })
                        
                        # Process team 2 (lay) odds - typically position 2 or 5
                        team2_odds_positions = [2, 5]  # Common positions for team2 odds
                        for pos in team2_odds_positions:
                            if pos < len(bet_cells):
                                cell = bet_cells[pos]
                                class_attr = cell.get_attribute('class') or ""
                                if "non" not in class_attr:
                                    price_elem = cell.query_selector('.c-bets__inner')
                                    if price_elem:
                                        price = price_elem.inner_text().strip()
                                        if price and price != '-':
                                            odds['lay'].append({
                                                'position': 0,
                                                'price': price,
                                                'volume': None
                                            })
                        
                        # Process draw odds if available - typically position 1 or 4
                        draw_odds_positions = [1, 4]  # Common positions for draw odds
                        for pos in draw_odds_positions:
                            if pos < len(bet_cells):
                                cell = bet_cells[pos]
                                class_attr = cell.get_attribute('class') or ""
                                if "non" not in class_attr:
                                    price_elem = cell.query_selector('.c-bets__inner')
                                    if price_elem:
                                        price = price_elem.inner_text().strip()
                                        if price and price != '-':
                                            # Add draw odds to a separate key
                                            match_data['draw_odds'] = price
                        
                        match_data['odds'] = odds
                        matches.append(match_data)
                except Exception as e:
                    logger.warning(f"Error processing cricket section with Playwright: {e}")
            
            if matches:
                logger.info(f"Extracted {len(matches)} cricket matches with Playwright")
                # Reset error count on successful extraction
                self.error_count = 0
            else:
                logger.warning("No cricket matches found with Playwright")
                self.error_count += 1
            
            return matches
        except Exception as e:
            logger.error(f"Error extracting cricket odds with Playwright: {e}")
            self.error_count += 1
            return []
    
    def _match_equal(self, old_match: Dict[str, Any], new_match: Dict[str, Any]) -> bool:
        """Compare two match objects to determine if they are equivalent"""
        # Keys to exclude when comparing (these can change without being considered a "change")
        exclude_keys = {'timestamp', 'id'}
        
        # Helper function to normalize volume strings for comparison
        def normalize_volume(vol_str):
            if not vol_str:
                return None
            # Remove commas and convert to numeric value for comparison
            return vol_str.replace(',', '')
        
        # Helper function to compare odds
        def odds_equal(odds1, odds2):
            if not odds1 and not odds2:
                return True
            if not odds1 or not odds2:
                return False
            
            # Compare back odds
            back1 = sorted(odds1.get('back', []), key=lambda x: x.get('position', 0))
            back2 = sorted(odds2.get('back', []), key=lambda x: x.get('position', 0))
            
            if len(back1) != len(back2):
                return False
                
            for o1, o2 in zip(back1, back2):
                # Compare position and price (most important)
                if o1.get('position') != o2.get('position') or o1.get('price') != o2.get('price'):
                    return False
                
                # Compare normalized volumes
                vol1 = normalize_volume(o1.get('volume'))
                vol2 = normalize_volume(o2.get('volume'))
                if vol1 != vol2:
                    return False
            
            # Compare lay odds
            lay1 = sorted(odds1.get('lay', []), key=lambda x: x.get('position', 0))
            lay2 = sorted(odds2.get('lay', []), key=lambda x: x.get('position', 0))
            
            if len(lay1) != len(lay2):
                return False
                
            for o1, o2 in zip(lay1, lay2):
                # Compare position and price
                if o1.get('position') != o2.get('position') or o1.get('price') != o2.get('price'):
                    return False
                
                # Compare normalized volumes
                vol1 = normalize_volume(o1.get('volume'))
                vol2 = normalize_volume(o2.get('volume'))
                if vol1 != vol2:
                    return False
            
            return True
        
        # Compare all keys except the excluded ones and odds
        for key in set(old_match.keys()) | set(new_match.keys()):
            if key in exclude_keys or key == 'odds':
                continue
            
            if key not in old_match or key not in new_match:
                return False
            
            if old_match[key] != new_match[key]:
                return False
        
        # Compare odds separately
        return odds_equal(old_match.get('odds'), new_match.get('odds'))
    
    def _detect_changes(self, old_match: Dict[str, Any], new_match: Dict[str, Any]) -> Dict[str, bool]:
        """Detect specific changes between two match objects"""
        changes = {
            "odds_changed": False,
            "score_changed": False,
            "status_changed": False
        }
        
        # Check for in_play status change
        if old_match.get('in_play') != new_match.get('in_play'):
            changes["status_changed"] = True
        
        # Check for score changes
        old_score = old_match.get('score')
        new_score = new_match.get('score')
        if (old_score is None and new_score is not None) or \
           (old_score is not None and new_score is None) or \
           (old_score != new_score):
            changes["score_changed"] = True
        
        # Helper function to check if odds have changed
        def odds_changed(old_odds, new_odds):
            if not old_odds and not new_odds:
                return False
                
            if bool(old_odds) != bool(new_odds):
                return True
                
            # Check if back odds changed
            old_back = sorted(old_odds.get('back', []), key=lambda x: x.get('position', 0))
            new_back = sorted(new_odds.get('back', []), key=lambda x: x.get('position', 0))
            
            if len(old_back) != len(new_back):
                return True
                
            for i, (old_item, new_item) in enumerate(zip(old_back, new_back)):
                # Compare prices
                if old_item.get('price') != new_item.get('price'):
                    return True
                
                # Compare volumes (normalize by removing commas)
                old_vol = old_item.get('volume', '').replace(',', '') if old_item.get('volume') else ''
                new_vol = new_item.get('volume', '').replace(',', '') if new_item.get('volume') else ''
                
                if old_vol != new_vol:
                    return True
            
            # Check if lay odds changed
            old_lay = sorted(old_odds.get('lay', []), key=lambda x: x.get('position', 0))
            new_lay = sorted(new_odds.get('lay', []), key=lambda x: x.get('position', 0))
            
            if len(old_lay) != len(new_lay):
                return True
                
            for i, (old_item, new_item) in enumerate(zip(old_lay, new_lay)):
                # Compare prices
                if old_item.get('price') != new_item.get('price'):
                    return True
                
                # Compare volumes
                old_vol = old_item.get('volume', '').replace(',', '') if old_item.get('volume') else ''
                new_vol = new_item.get('volume', '').replace(',', '') if new_item.get('volume') else ''
                
                if old_vol != new_vol:
                    return True
            
            return False
        
        # Check for odds changes
        old_odds = old_match.get('odds', {})
        new_odds = new_match.get('odds', {})
        
        if odds_changed(old_odds, new_odds):
            changes["odds_changed"] = True
        
        return changes
    
    def update_global_state(self, new_matches):
        """Update the global state with new matches data, tracking changes and ID mapping"""
        try:
            changes_made = 0
            current_time = datetime.now().isoformat()
            
            with scraper_state["lock"]:
                # Get current matches and ID mapping
                current_matches = scraper_state["data"].get("matches", [])
                id_mapping = scraper_state.get("id_mapping", {})
                match_history = scraper_state.get("match_history", {})
                
                # Build a dictionary of current matches by ID
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
                        # Check if the match has materially changed
                        if not self._match_equal(current_match, new_match):
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
                            
                            logger.debug(f"Updated match: {new_match['id']} - {changes}")
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
                        
                        logger.debug(f"New match added: {new_match['id']}")
                
                # When in recovery mode after errors, don't remove matches
                keep_existing = self.error_count > self.max_continuous_errors / 2
                
                # Check for removed matches, but don't remove if we're in recovery mode
                if not keep_existing:
                    for old_id, old_match in current_matches_by_id.items():
                        if old_id not in processed_ids:
                            # Match was removed
                            changes_made += 1
                            logger.debug(f"Match removed: {old_id}")
                else:
                    # In recovery mode, keep all existing matches that weren't updated
                    for old_id, old_match in current_matches_by_id.items():
                        if old_id not in processed_ids:
                            updated_matches.append(old_match)
                            logger.debug(f"Kept existing match in recovery mode: {old_id}")
                
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
                
                # Save data to files
                self._save_data_files(output_data, id_mapping)
                
                logger.info(f"Data updated with {changes_made} changes ({len(updated_matches)} matches)")
                return True
        except Exception as e:
            logger.error(f"Error updating global state: {e}")
            self.error_count += 1
            return False
    
    def _save_data_files(self, output_data, id_mapping):
        """Save data to files with error handling"""
        try:
            # Save the main data file
            temp_data_file = f"{DATA_FILE}.tmp"
            with open(temp_data_file, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False)
            
            # Atomic rename to prevent corruption
            os.replace(temp_data_file, DATA_FILE)
            
            # Save ID mapping file
            temp_id_file = f"{ID_MAPPING_FILE}.tmp"
            with open(temp_id_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'updated': datetime.now().isoformat(),
                    'mapping': id_mapping
                }, f, indent=2, ensure_ascii=False)
            
            # Atomic rename
            os.replace(temp_id_file, ID_MAPPING_FILE)
            
            return True
        except Exception as e:
            logger.error(f"Error saving data files: {e}")
            return False
    
    def run(self, interval=2):
        """Run the scraper every 'interval' seconds"""
        # Update scraper state
        with scraper_state["lock"]:
            scraper_state["is_running"] = True
            scraper_state["start_time"] = datetime.now()
            scraper_state["status"] = "starting"
        
        logger.info(f"Starting cricket odds scraper with {interval} second intervals")
        
        if not self.setup_driver():
            logger.error("Failed to set up browser driver. Exiting.")
            with scraper_state["lock"]:
                scraper_state["is_running"] = False
                scraper_state["status"] = "failed"
            return
        
        try:
            refresh_count = 0
            max_extractions_before_refresh = 15  # Refresh page completely every ~30 seconds (with 2s interval)
            
            # Navigate to the site initially
            if not self.navigate_to_site():
                logger.error("Failed to navigate to the website. Retrying setup...")
                
                # Try with longer timeout
                self.navigation_timeout = self.navigation_timeout * 2
                logger.info(f"Increasing timeout to {self.navigation_timeout} seconds")
                
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
                    
                    # Try switching to Playwright if we're using Selenium
                    if not self.use_playwright and PLAYWRIGHT_AVAILABLE:
                        logger.info("Switching to Playwright after multiple Selenium errors")
                        self.use_playwright = True
                        if self._setup_playwright() and self.navigate_to_site():
                            logger.info("Successfully switched to Playwright")
                            self.error_count = 0
                            continue
                        else:
                            logger.error("Failed to switch to Playwright")
                    
                    # Try switching back to Selenium if Playwright is failing
                    if self.use_playwright:
                        logger.info("Switching back to Selenium after Playwright errors")
                        self.use_playwright = False
                        if self.setup_driver() and self.navigate_to_site():
                            logger.info("Successfully switched back to Selenium")
                            self.error_count = 0
                            continue
                        else:
                            logger.error("Failed to switch back to Selenium")
                            
                    # If still failing, wait longer before retrying
                    logger.error("Both Selenium and Playwright failed. Waiting 60 seconds...")
                    time.sleep(60)
                    
                    # Try one more reset
                    if not self.setup_driver() or not self.navigate_to_site():
                        logger.error("Driver reset failed. Increasing wait time...")
                        time.sleep(120)  # Wait even longer before next try
                        continue
                    
                    self.error_count = 0
                
                # Check if we need to refresh the page
                if refresh_count >= max_extractions_before_refresh or force_refresh:
                    logger.info("Performing complete page refresh")
                    if not self.navigate_to_site():
                        logger.warning("Page refresh failed, attempting to reset driver")
                        if not self.setup_driver() or not self.navigate_to_site():
                            logger.error("Driver reset failed. Waiting before retrying...")
                            time.sleep(30)  # Wait longer before retrying
                            continue
                    refresh_count = 0
                
                # Extract and update the data
                matches = self.extract_cricket_odds()
                
                if matches:
                    self.update_global_state(matches)
                    # If successful extraction after errors, log the recovery
                    if self.error_count > 0:
                        logger.info(f"Recovered after {self.error_count} errors")
                        self.error_count = 0
                
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
                if not self.use_playwright and self.driver:
                    self.driver.quit()
                    logger.info("WebDriver closed")
                elif self.use_playwright:
                    if self.browser:
                        self.browser.close()
                    if self.playwright:
                        self.playwright.stop()
                    logger.info("Playwright browser closed")
            except Exception as e:
                logger.error(f"Error closing browser: {e}")
            
            # Update scraper state
            with scraper_state["lock"]:
                scraper_state["is_running"] = False
                scraper_state["status"] = "stopped"

# Create a function to start the scraper in a background thread
def start_scraper_thread():
    if not scraper_state["is_running"]:
        try:
            # Set up debug directories
            os.makedirs("debug_html", exist_ok=True)
            
            # Create and start the thread
            scraper = CricketOddsScraper()
            thread = threading.Thread(target=scraper.run, args=(2,), daemon=True)
            thread.start()
            logger.info("Scraper thread started")
            return True
        except Exception as e:
            logger.error(f"Failed to start scraper thread: {e}")
            # Try to install additional dependencies if needed
            try:
                logger.info("Installing additional dependencies and trying again")
                subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", 
                                      "webdriver-manager", "playwright", "selenium"])
                
                # Try to install playwright browsers
                try:
                    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
                except Exception as e:
                    logger.warning(f"Failed to install Playwright browsers: {e}")
                
                # Try again with fresh scraper
                scraper = CricketOddsScraper()
                thread = threading.Thread(target=scraper.run, args=(2,), daemon=True)
                thread.start()
                logger.info("Scraper thread started after installing dependencies")
                return True
            except Exception as e2:
                logger.error(f"Failed to start scraper after installing dependencies: {e2}")
                return False
    else:
        logger.info("Scraper is already running")
        return False

# Load existing data if available
def load_existing_data():
    try:
        # Create data directory if it doesn't exist
        os.makedirs(DATA_DIR, exist_ok=True)
        
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

@app.get("/", tags=["Root"])
async def root():
    """Root endpoint with API information"""
    return {
        "name": "Cricket Odds API",
        "version": "3.0.0",
        "description": "API for real-time cricket odds from 1xbet",
        "endpoints": [
            {"path": "/matches", "description": "Get all cricket matches"},
            {"path": "/matches/{match_id}", "description": "Get a specific match by ID"},
            {"path": "/status", "description": "Get the scraper status"},
            {"path": "/refresh", "description": "Force a refresh of the data"},
            {"path": "/changes", "description": "Get changes for a specific match"}
        ]
    }

@app.get("/matches", response_model=List[Match], tags=["Matches"])
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
async def get_match(match_id: str, request: Request):
    """Get a specific cricket match by ID with automatic redirection for legacy IDs"""
    match, new_id = find_match_by_id(match_id)
    
    if match:
        # If we found the match using a mapped ID, redirect to the new endpoint
        if new_id and new_id != match_id:
            redirect_url = str(request.url).replace(match_id, new_id)
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_301_MOVED_PERMANENTLY)
        
        # Return the match directly
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

@app.get("/changes/{match_id}", tags=["Matches"])
async def get_match_changes(match_id: str):
    """Get the change history for a specific match"""
    # Find the match first to handle redirects
    match, new_id = find_match_by_id(match_id)
    
    if not match:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Match with ID {match_id} not found"
        )
    
    # Use the correct ID to look up history
    lookup_id = new_id if new_id else match_id
    
    with scraper_state["lock"]:
        history = scraper_state.get("match_history", {}).get(lookup_id, [])
    
    return {
        "match_id": lookup_id,
        "team1": match.get("team1"),
        "team2": match.get("team2"),
        "changes": history
    }

@app.post("/refresh", tags=["System"])
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

@app.post("/start", tags=["System"])
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

@app.post("/stop", tags=["System"])
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

# On startup
@app.on_event("startup")
async def startup_event():
    # Load existing data
    load_existing_data()
    
    # Initialize scraper state
    scraper_state["start_time"] = datetime.now()
    
    # Create required directories
    os.makedirs("debug_html", exist_ok=True)
    
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
