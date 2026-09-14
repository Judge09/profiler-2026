import os
import secrets

# Change this password before use
PASSWORD = "profiler2024"

# Secret key for session signing — change this in production
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# Database
DATABASE_URI = "sqlite:///profiler.db"

# Upload settings
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "app", "static", "uploads", "profile_pics")
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max upload

# App settings
DEBUG = False
HOST = "127.0.0.1"
PORT = 5000
