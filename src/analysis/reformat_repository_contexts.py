"""Render the existing nine repository panels at the manuscript's native width.

Input is the exported panel source data; no sampling or statistical changes.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import gaussian_kde

SKILL = Path.home() / '.codex/skills/nature-figure/scripts'
sys.path.insert(0, str(SKILL))
from audit_panel_alignment import require_matplotlib_panel_alignment

SMELLS = [
    ('no_dependency_cache', 'No dependency cache', 6988),
    ('overly_specific_cache_key', 'Overly specific cache key', 376),
    ('deprecated_cache_action', 'Deprecated cache action', 227),
]
FEATURES = [('workflow_count', 'Workflow count'), ('runs_90d', 'Workflow runs (90 d)'), ('commits', 'Commits')]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--repositories', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('rq1_representative_repository_contexts_split_violin_ccs.pdf'))
    args = parser.parse_args()
    df = pd.read_csv(args.source)
    repositories = pd.read_csv(args.repositories)
    assert len(repositories) == 19330 and repositories.repository_id.nunique() == 19330
    plt.rcParams.update({'font.family':'Arial', 'font.size':7.5, 'pdf.fonttype':42, 'svg.fonttype':'none', 'axes.linewidth':0.6})
    fig, grid = plt.subplots(3, 3, figsize=(336.8/72, 5.6))
    fig.subplots_adjust(left=0.11, right=0.985, bottom=0.095, top=0.83, hspace=1.23, wspace=0.53)
    fig.text(0.5,0.985,'Representative repository contexts of CCS',ha='center',va='top',fontsize=9,fontweight='bold')
    fig.text(0.5,0.947,'None: no detectable CCS (n = 8,895); Target: indicated CCS',ha='center',va='top',fontsize=7)
    panel_summaries=[]
    for row,(code,name,expected_n) in enumerate(SMELLS):
        top=grid[row,0].get_position().y1
        fig.text(0.11,top+0.062,f'{name} (target n = {expected_n:,})',fontsize=8,fontweight='bold',va='bottom')
        for col,(feature,title) in enumerate(FEATURES):
            ax=grid[row,col]
            subset=df[(df.target_smell==code)&(df.feature==feature)]
            raw=[subset[subset.group==group].raw_value.to_numpy(float) for group in ['global_no_detectable_smell','target_smell_present']]
            assert len(raw[0])==8895 and len(raw[1])==expected_n
            assert all(np.isfinite(v).all() and (v>=0).all() for v in raw)
            values=[np.log10(v+1) for v in raw]
            # Preserve the original global-axis scale and Scott-bandwidth KDE.
            feature_raw=repositories[feature].to_numpy(float)
            lower=np.log10(feature_raw.min()+1); upper=np.log10(feature_raw.max()+1)
            pad=max(0.05,0.04*(upper-lower)); limits=(max(0,lower-pad),upper+pad)
            y=np.linspace(*limits,512)
            for side,v,color,edge,x in [(-1,values[0],'#C7CDD3','#87919A',-0.16),(1,values[1],'#4C78A8','#315C85',0.16)]:
                density=gaussian_kde(v,bw_method='scott')(y)
                ax.fill_betweenx(y,0,side*0.43*density/density.max(),facecolor=color,edgecolor=edge,linewidth=.55,alpha=.80,zorder=2)
                q1,median,q3=np.quantile(v,[.25,.5,.75])
                ax.vlines(x,q1,q3,color='#202020',linewidth=1.5,zorder=4)
                ax.hlines([q1,q3],x-.07,x+.07,color='#202020',linewidth=.7,zorder=4)
                ax.scatter([x],[median],s=11,color='#151515',zorder=5)
                panel_summaries.append({'smell':code,'feature':feature,'group':'none' if side<0 else 'target','n':len(v),'q1':float(q1),'median':float(median),'q3':float(q3)})
            candidates=np.array([0,10,100,1000,10000,100000,1000000,10000000])
            ticks=candidates[candidates<=max(feature_raw.max()*1.1,10)]
            if len(ticks)<3:ticks=candidates[:3]
            ticklabels=[f'{v/1000000:g}M' if v>=1000000 else f'{v/1000:g}k' if v>=1000 else str(v) for v in ticks]
            ax.set_ylim(*limits);ax.set_yticks(np.log10(ticks+1),ticklabels)
            ax.set_xlim(-.52,.52);ax.set_xticks([-.29,.29],['None','Target'])
            ax.tick_params(axis='x',labelsize=7,pad=3,length=0)
            ax.tick_params(axis='y',labelsize=6.5,width=.45,length=2)
            ax.axvline(0,color='#555555',linewidth=.55,zorder=3)
            ax.grid(axis='y',color='#E4E8EB',linewidth=.5);ax.set_axisbelow(True)
            ax.spines['top'].set_visible(False);ax.spines['right'].set_visible(False)
            ax.set_title(title,fontsize=7.2,pad=5,fontweight='bold')
            ax.text(-.25,1.15,'abcdefghi'[row*3+col],transform=ax.transAxes,fontweight='bold',fontsize=8,ha='left',va='top')
    fig.text(.5,.025,'Count ticks shown on a log10(x + 1) scale; black marks: median and IQR.',ha='center',fontsize=6.5)
    fig.canvas.draw()
    base=args.output.with_suffix('')
    require_matplotlib_panel_alignment(fig,axes=list(grid.ravel()),panel_ids=list('abcdefghi'),row_groups=[list('abc'),list('def'),list('ghi')],column_groups=[list('adg'),list('beh'),list('cfi')],json_out=str(base)+'.alignment.json',overlay_svg=str(base)+'.alignment.svg',tolerance_pt=1.5,gutter_tolerance_pt=1.5,require_panel_labels=True,strict=True)
    fig.savefig(args.output)
    fig.savefig(base.with_suffix('.svg'))
    fig.savefig(base.with_suffix('.png'),dpi=300)
    record={'input_sha256':hashlib.sha256(args.source.read_bytes()).hexdigest(),'source_rows':len(df),'used_rows':sum(s['n'] for s in panel_summaries),'sampling':False,'transform':'log10(x + 1)','kde':'Gaussian, Scott bandwidth','summary':panel_summaries}
    Path(str(base)+'.data-audit.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    plt.close(fig)
    print(json.dumps({'rows':len(df),'used_rows':record['used_rows'],'output':str(args.output)}))

if __name__=='__main__':main()
