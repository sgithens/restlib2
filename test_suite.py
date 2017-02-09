import os
import sys

if __name__ == "__main__":
    apps = sys.argv[1:]

    if not apps:
        apps = [
            'tests',
        ]

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

    from django.core.management import execute_from_command_line
    execute_from_command_line(['manage.py', 'test', 'tests'])
