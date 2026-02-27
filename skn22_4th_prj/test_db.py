import os
import django
from django.db import connections
from django.db.utils import OperationalError

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "skn22_4th_prj.settings")
django.setup()

db_conn = connections["default"]
try:
    db_conn.cursor()
    print("Database connection successful!")
except OperationalError as e:
    print(f"Database connection failed: {e}")
except Exception as e:
    print(f"An error occurred: {e}")
