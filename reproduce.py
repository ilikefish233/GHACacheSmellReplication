"""Run all offline paper analyses in separate processes, preserving frozen outputs."""
from pathlib import Path
import argparse
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from artifact import ROOT, DataPaths, add_data_dir

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=ROOT/'results_reproduced')
    p.add_argument('--rebuild-groups',action='store_true')
    p.add_argument('--verify-collected-runs',action='store_true')
    add_data_dir(p)
    a=p.parse_args();out=a.output.resolve()
    paths=DataPaths(a.data_dir)
    from data_setup import require_data
    require_data(paths=paths)
    if a.verify_collected_runs:
        subprocess.run([sys.executable,str(ROOT/'RQ2/check_collected_runs.py'),
                        '--data-dir',str(paths.root),'--output',str(out/'rq2_collection_check')],
                       cwd=ROOT,check=True)
    jobs=[('Validation/analysis.py','validation'),('RQ1/analysis.py','rq1'),('RQ2/analysis.py','rq2'),('RQ2/RQ2_3/analysis.py','rq2_3')]
    for script,name in jobs:
        command=[sys.executable,str(ROOT/script),'--output',str(out/name)]
        if name != 'validation' and paths.mapping is not None:
            command.extend(['--data-dir', str(paths.root)])
        if name=='rq2' and a.rebuild_groups:command.append('--rebuild-groups')
        subprocess.run(command,cwd=ROOT,check=True)
    subprocess.run([sys.executable,str(ROOT/'verify.py'),'--results',str(out)],cwd=ROOT,check=True)

if __name__=='__main__':main()
