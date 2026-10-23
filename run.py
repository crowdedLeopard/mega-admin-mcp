"""Entry point for hosts that run the repository as-is (Azure App Service, plain VM).

Adds ./src to the import path and starts the server on $PORT (default 8000).
Startup command on App Service:  python run.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from megamcp.app import main  # noqa: E402

if __name__ == "__main__":
    main()
