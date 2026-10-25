"""Run the shared RQ1 analysis without duplicating its full data."""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1]/"analysis.py"),run_name="__main__")
