#!/usr/bin/env python3
"""
Streamlit App - Alias to AI Market Intelligence Dashboard
Run with: streamlit run streamlit_app.py
"""

import sys
import os

# Add dashboard directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'dashboard'))

# Import and run the dashboard
from app import *
