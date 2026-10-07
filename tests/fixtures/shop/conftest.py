import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config")
django.setup()
