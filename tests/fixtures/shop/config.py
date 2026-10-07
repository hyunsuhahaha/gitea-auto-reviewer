import os

SECRET_KEY = "fixture"
USE_TZ = True
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth", "shop"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.environ["SHOP_DB"]}}
ROOT_URLCONF = "shop.urls"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
ERP_LIVE_SEND = False
