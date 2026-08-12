import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.db import connection

print("Active DB Engine:", connection.vendor)
print("Active DB Name:", connection.settings_dict['NAME'])
print("Active DB Host:", connection.settings_dict.get('HOST', 'localhost'))
print("Active DB Port:", connection.settings_dict.get('PORT', '5432'))
print("Active DB User:", connection.settings_dict.get('USER', 'postgres'))
