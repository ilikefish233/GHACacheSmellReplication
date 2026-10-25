"""Validate grouped binary WLS against independent expanded-run calculations."""
import sys
from pathlib import Path
import unittest
import numpy as np
from scipy.stats import t

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as lpm
from engine import fit, covariance


class LPMTests(unittest.TestCase):
    def test_all_failed_exposure_group_matches_expanded_wls_and_cluster_scores(self):
        x = np.array([[1, 0, -1], [1, 0, 1], [1, 1, -1], [1, 1, 1],
                      [1, 0, .5], [1, 1, -.5]], float)
        n = np.array([8, 7, 9, 6, 5, 10])
        successes = np.array([6, 5, 0, 0, 3, 0])
        weights = np.array([1/15]*4+[1/5, 1/10])
        repos = np.array([0, 0, 1, 1, 2, 3])
        beta, bread, ok, _ = fit(x, n, successes, successes, weights)
        cov = covariance(x, n, successes, weights, repos, beta, bread)
        ex = np.repeat(x, n, axis=0)
        ew = np.repeat(weights, n)
        er = np.repeat(repos, n)
        y = np.concatenate([np.r_[np.ones(s), np.zeros(k-s)] for k, s in zip(n, successes)])
        expected = np.linalg.lstsq(ex*np.sqrt(ew[:, None]), y*np.sqrt(ew), rcond=None)[0]
        inv = np.linalg.inv(ex.T@(ew[:, None]*ex))
        scores = np.stack([np.sum(ex[er==g]*(ew*(y-ex@expected))[er==g, None], axis=0)
                           for g in range(4)])
        expected_cov = inv@scores.T@scores@inv*4/3
        self.assertTrue(ok)
        self.assertTrue(np.all(np.isfinite(beta)))
        np.testing.assert_allclose(beta, expected, atol=1e-12)
        np.testing.assert_allclose(cov, expected_cov, atol=1e-12)
        rows = lpm.inference(['intercept', lpm.TYPES[0], 'control'], beta, cov, 4)
        row = rows[1]
        self.assertAlmostEqual(row['cluster_robust_p'], 2*t.sf(abs(beta[1]/np.sqrt(cov[1, 1])), 3))
        self.assertAlmostEqual(row['probability_difference_pp'], 100*beta[1])
        self.assertNotIn('exponentiated_coefficient', row)

    def test_fdr_excludes_controls_and_unidentified_coefficients(self):
        names = ['intercept', *lpm.TYPES[:2], 'control']
        rows = lpm.inference(names, np.array([0., 3., 9., 5.]), np.eye(4), 100,
                             unidentified=[lpm.TYPES[1]])
        self.assertEqual(rows[1]['fdr_q'], rows[1]['cluster_robust_p'])
        self.assertIsNone(rows[2]['fdr_q'])
        self.assertIsNone(rows[3]['fdr_q'])


if __name__ == '__main__':
    unittest.main()
