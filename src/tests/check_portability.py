"""Deny access to the development workspace and network while running an entry point."""
from pathlib import Path
import argparse
import json
import os
import runpy
import sys
ROOT=Path(__file__).resolve().parents[2]
ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument('entry');ap.add_argument('arguments',nargs=argparse.REMAINDER)
a=ap.parse_args();sys.path.insert(0,str(ROOT))
blocked_root=Path(ROOT.anchor)
def guard(event,args):
    if event in {'socket.connect','socket.getaddrinfo'}:
        raise PermissionError('Offline artifact must not access the network')
    if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
        path=Path(os.fsdecode(args[0])).resolve()
        if path.is_relative_to(blocked_root) and not path.is_relative_to(ROOT):
            raise PermissionError('External development-workspace access: '+str(path))
sys.addaudithook(guard)
sys.argv=[str(ROOT/a.entry),*a.arguments]
runpy.run_path(str(ROOT/a.entry),run_name='__main__')
print('Portability guard passed: no development-workspace or network access')
