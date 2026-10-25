from pathlib import Path
import sys
import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon, Ellipse, PathPatch
sys.path.insert(0, str(Path.home()/'.codex/skills/nature-figure/scripts'))
from audit_panel_alignment import require_matplotlib_panel_alignment
mpl.rcParams.update({'font.family':'sans-serif','font.sans-serif':['Arial'],'font.size':8,'pdf.fonttype':42,'ps.fonttype':42,'svg.fonttype':'none'})
OUT=Path(__file__).parent/'study_overview_redesigned'
INK,MUTED,LINE='#243342','#526170','#62717E'
BLUE,TEAL='#21628A','#25786F'
fig,ax=plt.subplots(figsize=(4.685039,2.913386)) # 119 x 74 mm
fig.subplots_adjust(left=0,right=1,bottom=0,top=1)
ax.set(xlim=(0,119),ylim=(0,74)); ax.axis('off')
def text(x,y,s,size=8.1,weight='normal',color=INK,ha='center'):
    ax.text(x,y,s,fontsize=size,fontweight=weight,color=color,ha=ha,va='center',linespacing=1.25)
def line(points,color=LINE):
    xs,ys=zip(*points); ax.plot(xs,ys,color=color,lw=.65,solid_capstyle='round')
def arrow(points,color=LINE,design=False):
    if len(points)>2: line(points[:-1],color)
    ax.add_patch(FancyArrowPatch(points[-2],points[-1],arrowstyle='->' if design else '-|>',mutation_scale=7,lw=.65,color=color,shrinkA=0,shrinkB=0,linestyle=(0,(3,2)) if design else '-'))
def region(y,h,label):
    ax.add_patch(FancyBboxPatch((1,y),117,h,boxstyle='round,pad=0,rounding_size=1',fc='white',ec='#6D7985',lw=.6,linestyle=(0,(4,3))))
    text(59.5,y+h-4,label,size=8.5,weight='bold')
def box(x,y,w,h,title,body,accent=INK,fill='white'):
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=0,rounding_size=.6',fc=fill,ec=LINE,lw=.65))
    text(x+w/2,y+h-5,title,size=8.3,weight='bold',color=accent)
    text(x+w/2,y+(h-10)/2,body)
# A landscape schematic: data stores, document input, detector and RQ outputs.
# Distinct shapes and labels retain the reading order in monochrome.
def shape(vertices, fill='white', edge=LINE):
    ax.add_patch(Polygon(vertices,closed=True,fc=fill,ec=edge,lw=.65,joinstyle='round'))
def pill(x,y,w,h,fill):
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=0,rounding_size=3',
                               fc=fill,ec=LINE,lw=.65))
text(18,70,'a  Data construction',size=8.5,weight='bold')
text(58.5,70,'b  CCS detection',size=8.5,weight='bold')
text(101,70,'c  Analyses',size=8.5,weight='bold')
# Database cylinder: corpus sources.
ax.add_patch(FancyBboxPatch((3,48),30,13,boxstyle='square,pad=0',fc='#F1F5F8',ec='none'))
ax.add_patch(Ellipse((18,48),30,4,fc='#F1F5F8',ec=LINE,lw=.65))
line([(3,48),(3,61)]); line([(33,48),(33,61)])
ax.add_patch(Ellipse((18,61),30,4,fc='#E1EAF0',ec=LINE,lw=.65))
text(18,57,'GitHub Actions',size=8.3,weight='bold')
text(18,52,'GHALogs + API')
# Folded document: normalized and version-aligned cohorts.
shape([(3,12),(33,12),(33,33),(29,37),(3,37)],fill='#F7F9FB')
line([(29,37),(29,33),(33,33)])
text(18,30,'Version alignment',size=8.3,weight='bold')
text(18,22,'118,523 workflows\n51,179 versions\n1,093,639 runs')
arrow([(18,46),(18,37)])
# Rule-design evidence informs implementation; it is not detector input data.
pill(42,54,33,10,'#F7F9FB')
text(58.5,59,'Evidence for rules\nDocs, literature, cases')
shape([(45,31),(72,31),(76,40),(72,49),(45,49),(41,40)],fill='#EDF3F7')
text(58.5,44,'Static detector',size=8.3,weight='bold')
text(58.5,36.5,'11 CCS types\nYAML-based rules')
arrow([(58.5,54),(58.5,49)],design=True)
text(63,51.5,'Rule design',ha='left',color=MUTED)
arrow([(33,25),(37.5,25),(37.5,40),(41,40)])
pill(42,10,33,14,'#F7F9FB')
text(58.5,20,'Manual evaluation',size=8.3,weight='bold')
text(58.5,14,'200 reviewed\n176 eligible cases')
arrow([(58.5,31),(58.5,24)])
# Analysis modules use slanted boundaries rather than another row of cards.
shape([(83,42),(114,42),(118,64),(87,64)],fill='#F0F6FB',edge=BLUE)
text(100.5,59,'RQ1',size=8.5,weight='bold',color=BLUE)
text(100.5,50.5,'Prevalence; context\nMultiplicity and\nco-occurrence')
shape([(83,10),(114,10),(118,32),(87,32)],fill='#EFF7F5',edge=TEAL)
text(100.5,27,'RQ2',size=8.5,weight='bold',color=TEAL)
text(100.5,18.5,'Duration; success\nVM time and\nestimated cost')
line([(76,40),(79.5,40)])
arrow([(79.5,40),(79.5,53),(85,53)],BLUE)
arrow([(79.5,40),(79.5,21),(85,21)],TEAL)
# Line type, not color, identifies the relationship semantics.
arrow([(4,4),(12,4)])
text(14,4,'Data / outputs',ha='left',color=MUTED)
arrow([(48,4),(56,4)],design=True)
text(58,4,'Rule-design evidence',ha='left',color=MUTED)
fig.canvas.draw()
require_matplotlib_panel_alignment(fig,json_out=str(OUT)+'.alignment.json',overlay_svg=str(OUT)+'.alignment.svg',strict=True)
fig.savefig(str(OUT)+'.pdf',facecolor='white')
fig.savefig(str(OUT)+'.svg',facecolor='white')
fig.savefig(str(OUT)+'.png',dpi=600,facecolor='white')
fig.savefig(Path(__file__).parent.parent/'Fig1.eps',facecolor='white')
fig.savefig(Path(__file__).parent.parent/'Fig1.pdf',facecolor='white')
plt.close(fig)
