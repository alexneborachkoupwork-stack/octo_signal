"""
Central configuration. Copy .env.example to .env and fill in your values,
or set environment variables directly.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Octo Browser local API (always on this address when Octo is running) ---
OCTO_LOCAL_API = "http://127.0.0.1:58888"

# --- Profile to automate ---
# Set via env var or replace the placeholder here
PROFILE_UUID = os.getenv("OCTO_PROFILE_UUID", "REPLACE_WITH_YOUR_PROFILE_UUID")

# --- Target site ---
TARGET_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/"

# --- Test credentials (fill these before running) ---
TEST_USERNAME = os.getenv("TEST_USERNAME", "your_username@example.com")
TEST_PASSWORD = os.getenv("TEST_PASSWORD", "your_password")
