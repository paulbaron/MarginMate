#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    if sys.argv[1:2] == ["serve"]:
        # The production server never runs in debug, whatever .env says
        # (accounts/management/commands/serve.py). Set before the settings
        # load - load_dotenv never overrides a variable already set - so that
        # everything decided from DEBUG at load agrees: a missing or weak
        # SECRET_KEY is refused there.
        os.environ["DJANGO_DEBUG"] = "False"
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
