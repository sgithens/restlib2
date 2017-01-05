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
    from django.core.management import call_command

    # execute_from_command_line(sys.argv)
    execute_from_command_line(['manage.py', 'test', 'tests'])
    # call_command('test', *apps)
