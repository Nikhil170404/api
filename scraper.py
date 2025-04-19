# This should be the XbetScraper class from the previous code
# Copy the entire class definition, but remove the main execution code at the bottom
import time
import random
import json
import pandas as pd
import os
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

class XbetScraper:
    def __init__(self):
        self.base_url = "https://ind.1xbet.com/"
        
        # Setup Chrome options optimized for Render's free tier
        self.chrome_options = Options()
        self.chrome_options.add_argument("--headless")
        self.chrome_options.add_argument("--no-sandbox")
        self.chrome_options.add_argument("--disable-dev-shm-usage")
        self.chrome_options.add_argument("--disable-gpu")
        self.chrome_options.add_argument("--disable-extensions")
        self.chrome_options.add_argument("--disable-infobars")
        self.chrome_options.add_argument("--window-size=1366,768")
        self.chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
        
        # Reduce memory usage
        self.chrome_options.add_argument("--js-flags=--expose-gc")
        self.chrome_options.add_argument("--disable-backgrounding-occluded-windows")
        self.chrome_options.add_argument("--disable-backgrounding-suspended-windows")
        
        # Initialize WebDriver with ChromeDriverManager
        print("Setting up Chrome WebDriver...")
        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=self.chrome_options)
        self.wait = WebDriverWait(self.driver, 10)
        print("WebDriver initialized successfully")
    
    def __del__(self):
        """Close the browser when done"""
        if hasattr(self, 'driver'):
            try:
                self.driver.quit()
                print("WebDriver closed successfully")
            except:
                print("Error closing WebDriver")

    # [Copy all the other methods from the previous code without changes]
    # ...
