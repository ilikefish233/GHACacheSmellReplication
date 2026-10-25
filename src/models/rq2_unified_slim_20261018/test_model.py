"""Meaningful checks of grouped/expanded estimates, covariance and YAML controls."""
import unittest
import json
import tempfile
from pathlib import Path
import numpy as np
from engine import fit, covariance, bh
from features import extract
from run import TYPES, independent_columns, prepare


class ExactGroupingTests(unittest.TestCase):
    def setUp(self):
        self.x = np.array([[1,0,-1],[1,0,1],[1,1,-1],[1,1,1],[1,0,.5],[1,1,-.5]],float)
        self.count=np.array([8,7,9,6,5,10],float)
        self.weight=np.array([1/15,1/15,1/15,1/15,1/15,1/15])
        self.repo=np.array([0,0,1,1,2,2])
        self.y=np.array([2,5,3,4,2,7],float)
        self.expanded_x=np.repeat(self.x,self.count.astype(int),axis=0)
        self.expanded_w=np.repeat(self.weight,self.count.astype(int))
        self.expanded_repo=np.repeat(self.repo,self.count.astype(int))

    def test_linear_fit_and_cluster_covariance_match_expanded_runs(self):
        rng=np.random.default_rng(42)
        groups=[rng.normal(loc=x[1]+x[2],size=int(n)) for x,n in zip(self.x,self.count)]
        total=np.array([g.sum() for g in groups]); sq=np.array([(g*g).sum() for g in groups])
        expanded=np.concatenate(groups)
        b,h,_,_=fit(self.x,self.count,total,sq,self.weight)
        eb,eh,_,_=fit(self.expanded_x,np.ones(len(expanded)),expanded,expanded**2,self.expanded_w)
        np.testing.assert_allclose(b,eb,rtol=1e-11,atol=1e-11)
        c=covariance(self.x,self.count,total,self.weight,self.repo,b,h)
        ec=covariance(self.expanded_x,np.ones(len(expanded)),expanded,self.expanded_w,self.expanded_repo,eb,eh)
        np.testing.assert_allclose(c,ec,rtol=1e-11,atol=1e-11)

    def test_binomial_fit_and_cluster_covariance_match_expanded_runs(self):
        expanded=np.concatenate([np.r_[np.ones(int(y)),np.zeros(int(n-y))] for y,n in zip(self.y,self.count)])
        b,h,ok,_=fit(self.x,self.count,self.y,np.zeros(6),self.weight,True)
        eb,eh,eok,_=fit(self.expanded_x,np.ones(len(expanded)),expanded,np.zeros(len(expanded)),self.expanded_w,True)
        self.assertTrue(ok and eok)
        np.testing.assert_allclose(b,eb,rtol=1e-10,atol=1e-10)
        c=covariance(self.x,self.count,self.y,self.weight,self.repo,b,h,True)
        ec=covariance(self.expanded_x,np.ones(len(expanded)),expanded,self.expanded_w,self.expanded_repo,eb,eh,True)
        np.testing.assert_allclose(c,ec,rtol=1e-10,atol=1e-10)

    def test_version_weights_still_sum_to_one_after_grouping(self):
        for ids in ([0,1],[2,3],[4,5]):
            self.assertAlmostEqual(sum(self.count[ids]*self.weight[ids]),1.)

    def test_cluster_resampling_matches_expansion(self):
        multiples=np.array([2,0,1])
        total=self.y; sq=self.y**2/self.count
        b,_,_,_=fit(self.x,self.count,total,sq,self.weight*multiples[self.repo])
        expanded=np.repeat(total/self.count,self.count.astype(int))
        eb,_,_,_=fit(self.expanded_x,np.ones(len(expanded)),expanded,expanded**2,self.expanded_w*multiples[self.expanded_repo])
        np.testing.assert_allclose(b,eb,rtol=1e-10,atol=1e-10)

    def test_bh_only_corrects_available_hypotheses(self):
        q=bh({'a':.01,'b':.04,'absent':None})
        self.assertEqual(q['a'],.02); self.assertEqual(q['b'],.04); self.assertIsNone(q['absent'])

    def test_aliases_not_reported_as_identifiable_exposures(self):
        x=np.array([[1,0,0],[1,1,1],[1,0,0],[1,1,1]],float)
        _,_,audit=independent_columns(x,['intercept',TYPES[0],TYPES[1]])
        self.assertEqual(set(audit['unidentified_smells']),set(TYPES[:2]))


class FeatureTests(unittest.TestCase):
    def test_feature_cache_resume_preserves_all_population_and_endpoint_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); inputs=root/'inputs'; cached=root/'cached'; out=root/'out'
            for p in (inputs,cached,out): p.mkdir()
            feature=extract('jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo test\n')
            states=[dict(version_id='v'+str(i),repository_id=str(i),repository_name='owner/repo',workflow_path='ci.yml',content_hash='h'+str(i),detector=dict(smells=[])) for i in range(2)]
            (inputs/'version_scan_results.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in states),encoding='utf-8')
            (cached/'workflow_features.jsonl').write_text(''.join(json.dumps(dict(version_id=r['version_id'],content_hash=r['content_hash'],**feature))+'\n' for r in states),encoding='utf-8')
            runs=[dict(version_id='v0',status='completed',created_at='2026-06-01T00:00:00Z',event='push',conclusion='success',duration_seconds=10),dict(version_id='v1',status='completed',created_at='2026-06-01T00:00:00Z',event='push',conclusion='failure',duration_seconds=20)]
            (inputs/'run_records.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in runs),encoding='utf-8')
            versions,keys,values,success=prepare(inputs,out,1,{},cached)
            self.assertEqual(len(versions),2)
            self.assertEqual(sum(values[:,0]),2)
            self.assertEqual(sum(values[:,2]),2)
            self.assertEqual(sum(success),1)
            self.assertEqual(len(list(out.glob('workflow_features.jsonl'))),1)

    def test_matrix_runners_cache_exclusion_and_linux_not_a_function(self):
        data=extract('''name: linux
jobs:
  build:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
    steps:
      - uses: actions/cache@v4
      - uses: actions/setup-node@v4
      - run: npm ci
''')
        self.assertEqual(data['job_count'],1)
        self.assertEqual(data['non_cache_step_count'],2)
        self.assertEqual(data['runner_os'],'mixed')
        self.assertEqual(data['runner_hosting'],'github_hosted')
        self.assertEqual(data['workflow_role'],'build')
        self.assertEqual(data['ecosystem'],'javascript')
        self.assertEqual(data['has_matrix'],1)

    def test_reusable_workflow_is_unknown_not_zero_evidence(self):
        data=extract('jobs:\n  pipeline:\n    uses: owner/repo/.github/workflows/ci.yml@main\n')
        self.assertEqual(data['ecosystem'],'unknown')
        self.assertEqual(data['runner_os'],'unknown')
        self.assertTrue(data['reusable_job_present'])


if __name__=='__main__': unittest.main()
