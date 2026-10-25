"""Update the existing two-panel RQ2 forest style with unified model estimates."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.ticker import NullFormatter, NullLocator

SKILL = Path.home()/'.codex/skills/nature-figure/scripts'
sys.path.insert(0, str(SKILL))
from audit_panel_alignment import require_matplotlib_panel_alignment

SMELL_LABELS = {
    'no_dependency_cache': 'No dependency cache',
    'no_fallback_cache': 'No fallback cache',
    'redundant_cache_update': 'Redundant cache update',
    'cache_position_error': 'Cache position error',
    'conflicting_cache_sources': 'Overlapping cache mechanisms',
    'static_cache_key': 'Static cache key',
    'overly_specific_cache_key': 'Overly specific cache key',
    'no_docker_layer_cache': 'No Docker layer cache',
    'deprecated_cache_action': 'Deprecated cache action',
    'cache_as_artifact': 'Cache as artifact',
}
ORDER = list(SMELL_LABELS)


def build_forest(report, output_stem):
    mpl.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': ['Arial', 'Helvetica', 'DejaVu Sans', 'sans-serif'],
        'svg.fonttype': 'none', 'pdf.fonttype': 42,
        'font.size': 9.0, 'axes.spines.right': False, 'axes.spines.top': False,
        'axes.linewidth': .7, 'legend.frameon': False,
    })
    # Keep the original panel geometry, colors, category order and marker encoding.
    fig, axes = plt.subplots(1, 2, figsize=(336.8/72, 3.85), sharey=True)
    y_positions = list(range(len(ORDER)))[::-1]
    plotted = []
    specifications = [
        ('all_completed_duration', 'Duration ratio', 'a  Completed-run\nduration', '#2B6CB0'),
        ('reliability', 'Success difference\n(percentage points)', 'b  Workflow-run\nsuccess', '#C05621'),
    ]
    for axis, (endpoint, xlabel, title, color) in zip(axes, specifications):
        duration = endpoint == 'all_completed_duration'
        axis.axvline(1. if duration else 0., color='#6B7280', linewidth=.8, linestyle='--', zorder=0)
        coefficients = {r['feature']: r for r in report[endpoint]['coefficients']}
        for y, name in zip(y_positions, ORDER):
            row = coefficients[name]
            estimate = row['exponentiated_coefficient'] if duration else row['probability_difference_pp']
            low, high = row['exponentiated_ci_95'] if duration else row['probability_difference_pp_ci_95']
            if duration and (estimate <= 0 or low <= 0 or high <= 0):
                raise ValueError('Duration ratios must be positive on the log axis')
            if not low <= estimate <= high:
                raise ValueError('Invalid interval')
            significant = row['fdr_q'] < .05
            marker = 's' if significant else 'o'
            axis.errorbar(estimate, y, xerr=[[estimate-low], [high-estimate]],
                fmt=marker, markersize=3.8, markerfacecolor=color, markeredgecolor=color,
                markeredgewidth=.9, ecolor=color, elinewidth=1., capsize=2., zorder=3)
            plotted.append(dict(endpoint=endpoint, smell_type=name, estimate=estimate,
                ci95_low=low, ci95_high=high, bh_q=row['fdr_q'], significant=significant))
        if duration:
            axis.set_xscale('log')
            axis.set_xlim(.125, 4.2)
            axis.set_xticks([.25,.5,1,2,4], labels=['0.25','0.5','1','2','4'])
        else:
            # LPM differences have a zero null; a logarithmic OR axis is invalid.
            # Keep one continuous linear axis and show the entire deprecated-action CI.
            axis.set_xlim(-90,12)
            axis.set_xticks([-80,-40,0], labels=['-80','-40','0'])
        axis.xaxis.set_minor_locator(NullLocator())
        axis.xaxis.set_minor_formatter(NullFormatter())
        axis.set_ylim(-.75,len(ORDER)-.25)
        axis.set_xlabel(xlabel,fontsize=7.5)
        axis.tick_params(axis='x',labelsize=7)
        axis.set_title(title,loc='left',fontweight='bold',fontsize=8.,pad=7)
    axes[0].set_yticks(y_positions,labels=list(SMELL_LABELS.values()))
    axes[0].tick_params(axis='y',length=0,pad=6,labelsize=7)
    axes[1].tick_params(axis='y',length=0)
    fig.subplots_adjust(left=.43,right=.985,top=.86,bottom=.15,wspace=.28)
    fig.canvas.draw()
    output_stem.parent.mkdir(parents=True,exist_ok=True)
    require_matplotlib_panel_alignment(fig,json_out=str(output_stem)+'.alignment.json',
        overlay_svg=str(output_stem)+'.alignment.svg',tolerance_pt=1.5,gutter_tolerance_pt=1.5,strict=True)
    fig.savefig(str(output_stem)+'.pdf',bbox_inches='tight')
    fig.savefig(str(output_stem)+'.svg',bbox_inches='tight')
    fig.savefig(str(output_stem)+'.png',dpi=600,bbox_inches='tight')
    fig.savefig(str(output_stem)+'.tiff',dpi=600,bbox_inches='tight',pil_kwargs={'compression':'tiff_lzw'})
    plt.close(fig)
    with Path(str(output_stem)+'.data.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(plotted[0]));writer.writeheader();writer.writerows(plotted)
    assert sum(r['significant'] for r in plotted if r['endpoint']=='all_completed_duration')==4
    assert sum(r['significant'] for r in plotted if r['endpoint']=='reliability')==3
    # Every plotted estimate is checked against the main-table rounding.
    expected={'no_dependency_cache':(1.23,-2.42),'no_fallback_cache':(1.12,-2.23),
              'no_docker_layer_cache':(1.75,-.92),'deprecated_cache_action':(.47,-82.52)}
    for name, pair in expected.items():
        for endpoint,value in zip(['all_completed_duration','reliability'],pair):
            row=next(r for r in plotted if r['smell_type']==name and r['endpoint']==endpoint)
            assert round(row['estimate'],2)==value
    return plotted


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results',type=Path,default=Path(__file__).with_name('rq2_adjusted_associations_forest.source.json'))
    parser.add_argument('--output',type=Path,default=Path(__file__).with_name('rq2_adjusted_associations_forest'))
    args=parser.parse_args()
    report=json.loads(args.results.read_text(encoding='utf-8'))
    data=build_forest(report,args.output)
    print(json.dumps(dict(plotted_coefficients=len(data),duration_significant=4,success_significant=3)))
