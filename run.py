#!/usr/bin/env python3
"""
Simple runner script for Canopy.

This script provides a convenient way to start Canopy without
having to use the python -m canopy.main command.
"""

import sys
import os

# Add the current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canopy.main import main

if __name__ == '__main__':
    main()