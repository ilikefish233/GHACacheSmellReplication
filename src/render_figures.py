"""Re-render the adopted quantitative panels with their original Python designs."""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from artifact import ROOT,new_output,write,DataPaths,add_data_dir

def module(name,path):
    source=path.read_text(encoding='utf-8-sig')
    lines=source.splitlines()
    lines=[line for line in lines if not line.startswith('SKILL =') and not line.startswith('sys.path.insert(0, str(SKILL))')]
    source='\n'.join(lines)+'\n'
    # These are exactly the run3 manuscript's updated sample counts.
    for old,new in [('6988','6976'),('376','405'),('227','249'),('19330','19324'),('8895','8796'),('8,895','8,796')]:
        if name=='repository':source=source.replace(old,new)
    scope={'__name__':name,'__file__':str(path)}
    exec(compile(source,str(path),'exec'),scope)
    return scope

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--results',type=Path,help='Optional regenerated RQ2 results directory; default uses frozen adopted results')
    add_data_dir(ap)
    a=ap.parse_args();out=new_output(a.output)
    sys.path.insert(0,str(ROOT/'src/vendor'))
    forest=module('forest',ROOT/'src/analysis/reformat_rq2_forest.py')
    duration=a.results/'all_completed_duration.json' if a.results else ROOT/'RQ2/expected/duration/all_completed_duration.json'
    success=a.results/'reliability_lpm.json' if a.results else ROOT/'RQ2/expected/success/reliability_lpm.json'
    report={'all_completed_duration':json.loads(duration.read_text(encoding='utf-8')),
        'reliability':json.loads(success.read_text(encoding='utf-8'))}
    forest['build_forest'](report,out/'rq2_adjusted_associations_forest')
    repository=module('repository',ROOT/'src/analysis/reformat_repository_contexts.py')
    data=DataPaths(a.data_dir).path('RQ1/paper_source_data')
    for name in ('repository_feature_comparison_long_data.csv', 'repository_source_data.csv'):
        if not (data/name).is_file():
            ap.error(f'Missing RQ1 figure input: {data/name}')
    sys.argv=['repository','--source',str(data/'repository_feature_comparison_long_data.csv'),
        '--repositories',str(data/'repository_source_data.csv'),'--output',str(out/'rq1_representative_repository_contexts_split_violin_ccs.pdf')]
    repository['main']()
    write(out/'figure_contract.json',{'backend':'python','design':'original manuscript panels retained',
        'evidence':'frozen or regenerated adjusted associations; all eligible repository observations',
        'interpretation':'observational associations and descriptive repository contrasts',
        'uncertainty':'pointwise repository-cluster intervals; significance by endpoint BH q',
        'repository_transform':'log10(x+1); Gaussian KDE with Scott bandwidth; median and IQR',
        'sampling':False,'export':['pdf','svg','png'],'alignment_gate':True})

if __name__=='__main__':main()
