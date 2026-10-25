"""Optional live prototype UI, explicitly bound to the paper's frozen detector."""
from pathlib import Path
import runpy
import sys
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(HERE))
sys.path.insert(0,str(ROOT/'src/frozen'))
runpy.run_module('workflow_smell_web',run_name='__main__')
