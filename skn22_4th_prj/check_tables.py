import os
import django
from django.db import connection

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "skn22_4th_prj.settings")
django.setup()

with connection.cursor() as cursor:
    cursor.execute("SHOW TABLES")
    tables = cursor.fetchall()
    print("Tables in database:")
    for t in tables:
        print(f"- {t[0]}")
