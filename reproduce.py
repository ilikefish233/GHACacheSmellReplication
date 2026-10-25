"""Run all offline paper analyses in separate processes, preserving frozen outputs."""
from pathlib import Path
import argparse
import subprocess
import sys
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from artifact import ROOT

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=ROOT/'results_reproduced')
    p.add_argument('--rebuild-groups',action='store_true');a=p.parse_args();out=a.output.resolve()
    from data_setup import require_data
    require_data()
    jobs=[('Validation/analysis.py','validation'),('RQ1/analysis.py','rq1'),('RQ2/analysis.py','rq2'),('RQ2/RQ2_3/analysis.py','rq2_3')]
    for script,name in jobs:
        command=[sys.executable,str(ROOT/script),'--output',str(out/name)]
        if name=='rq2' and a.rebuild_groups:command.append('--rebuild-groups')
        subprocess.run(command,cwd=ROOT,check=True)
    subprocess.run([sys.executable,str(ROOT/'verify.py'),'--results',str(out)],cwd=ROOT,check=True)

if __name__=='__main__':main()
