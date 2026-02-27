import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR.parent / ".env"
print(f"Loading env from: {env_path}")
print(f"Exists: {env_path.exists()}")

load_dotenv(env_path)
key = os.getenv("OPENAI_API_KEY")
print(f"OPENAI_API_KEY: {key[:10]}..." if key else "OPENAI_API_KEY: NOT FOUND")

db_user = os.getenv("DB_USER")
print(f"DB_USER: {db_user}")
