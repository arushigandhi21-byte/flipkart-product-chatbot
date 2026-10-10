"""Verify the notebook's Selenium browser without scraping a website."""
import os
from pathlib import Path

from selenium import webdriver

ROOT = Path(__file__).resolve().parent.parent
os.environ["SE_CACHE_PATH"] = str(ROOT / ".cache" / "selenium")
os.environ["SE_AVOID_STATS"] = "true"


if __name__ == "__main__":
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    with webdriver.Chrome(options=options) as driver:
        driver.get("data:text/html,<title>Project browser check</title>")
        assert driver.title == "Project browser check"
        print(f"Selenium browser: OK (Chrome {driver.capabilities['browserVersion']})")
