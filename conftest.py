"""
pytest configuration — adds the repo root to sys.path so all packages
are importable without installation.
"""
import sys
import os

# Ensure the repo root is on the path
sys.path.insert(0, os.path.dirname(__file__))
