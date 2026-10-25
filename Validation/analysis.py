"""Re-evaluate the paper's 176 eligible cases with the run3 detector."""
from pathlib import Path
import argparse
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from artifact import ROOT,new_output,write,canonical
import json

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',type=Path,required=True)
    a=ap.parse_args();out=new_output(a.output)
    path=ROOT/'src/analysis/evaluate_frozen_run3.py'
    s=path.read_text(encoding='utf-8-sig')
    lines=s.splitlines()
    lines=["sys.path.insert(0, "+repr(str(ROOT/'src/frozen'))+")" if line.startswith('sys.path.insert(0,') else line for line in lines]
    lines=["    out_file = "+repr(str(out/'evaluation.json')) if line.strip().startswith('out_file =') else line for line in lines]
    s='\n'.join(lines)+'\n'
    sys.argv=[str(path),'--xlsx',str(ROOT/'Validation/data/annotations.xlsx'),'--limit','200']
    exec(compile(s,str(path),'exec'),{'__name__':'__main__','__file__':str(path)})
    write(out/'evaluation.json',canonical(json.loads((out/'evaluation.json').read_text(encoding='utf-8'))))

if __name__=='__main__':main()
