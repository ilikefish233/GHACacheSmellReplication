"""Run the original model-equivalence tests with a workspace-local temp directory."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
scratch=ROOT/'.test_tmp';scratch.mkdir(exist_ok=True)
tempfile.tempdir=str(scratch)
sys.path.insert(0,str(ROOT/'src/models/rq2_unified_slim_20261018'))
suite=unittest.defaultTestLoader.discover(str(ROOT/'src/models/rq2_unified_slim_20261018'),pattern='test_model.py')
result=unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():sys.exit(1)
test=ROOT/'src/models/rq2_unified_lpm_20261019/test_lpm.py'
bootstrap='import sys,runpy; sys.path.insert(0,'+repr(str(ROOT))+'); runpy.run_path('+repr(str(test))+',run_name="__main__")'
subprocess.run([sys.executable,'-c',bootstrap],cwd=ROOT,check=True)
