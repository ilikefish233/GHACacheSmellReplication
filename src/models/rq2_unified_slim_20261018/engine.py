"""Exact sufficient-statistic fitting and repository-cluster sandwich inference.

Identical design rows are grouped, never averaged across different predictors.
Cluster scores and endpoint-specific version weights reproduce expanded rows.
"""
import math

import numpy as np
from scipy.special import expit
from scipy.stats import norm


def fit(x, count, total, total_sq, unit_weight, logistic=False, initial=None, max_iter=100):
    mass = count * unit_weight
    if not logistic:
        bread = x.T @ (x * mass[:, None])
        beta = np.linalg.solve(bread, x.T @ (total * unit_weight))
        return beta, bread, True, 1
    beta = np.zeros(x.shape[1]) if initial is None else initial.copy()
    def likelihood(b):
        eta = x @ b
        return float(np.sum(unit_weight * (total * eta - count * np.logaddexp(0, eta))))
    converged = False
    for iteration in range(max_iter):
        eta = x @ beta
        prob = expit(eta)
        variance = mass * prob * (1-prob)
        bread = x.T @ (x * variance[:, None])
        score = x.T @ ((total-count*prob)*unit_weight)
        try:
            delta = np.linalg.solve(bread, score)
        except np.linalg.LinAlgError:
            return beta, bread, False, iteration+1
        if np.max(np.abs(delta)) < 1e-8:
            converged = True
            break
        old = likelihood(beta)
        scale = 1.
        while scale > 1e-8 and likelihood(beta + scale*delta) < old-1e-10:
            scale *= .5
        if scale <= 1e-8:
            break
        beta += scale*delta
    prob = expit(x @ beta)
    bread = x.T @ (x * (mass*prob*(1-prob))[:, None])
    return beta, bread, converged, iteration+1


def covariance(x, count, total, weight, repo_codes, beta, bread, logistic=False):
    fitted = expit(x @ beta) if logistic else x @ beta
    score = weight*(total-count*fitted)
    groups = int(np.max(repo_codes))+1
    scores = np.zeros((groups, x.shape[1]))
    np.add.at(scores, repo_codes, x*score[:, None])
    inverse = np.linalg.inv(bread)
    cov = inverse @ (scores.T @ scores) @ inverse
    if groups > 1:
        cov *= groups/(groups-1.)
    return (cov+cov.T)/2


def bh(values):
    items = sorted((p, key) for key,p in values.items() if p is not None and np.isfinite(p))
    result = {key: None for key in values}
    current = 1.
    for index in range(len(items)-1, -1, -1):
        p, key = items[index]
        current = min(current, p*len(items)/(index+1))
        result[key] = current
    return result


def coefficient_rows(names, beta, cov, smell_types, suppress=False):
    result = []
    for i,name in enumerate(names):
        se = math.sqrt(max(0., float(cov[i,i])))
        interval = [float(beta[i]-1.96*se), float(beta[i]+1.96*se)]
        p = float(2*norm.sf(abs(beta[i]/se))) if se > 0 else None
        result.append(dict(feature=name, coefficient=float(beta[i]), standard_error=se,
                           coefficient_ci_95=interval, exponentiated_coefficient=float(np.exp(np.clip(beta[i], -700,700))),
                           exponentiated_ci_95=[float(np.exp(np.clip(v,-700,700))) for v in interval],
                           cluster_robust_p=None if suppress else p,
                           inference_status='suppressed_model_separation_or_nonconvergence' if suppress else 'repository_cluster_robust'))
    q = bh({r['feature']:r['cluster_robust_p'] for r in result if r['feature'] in smell_types})
    for row in result:
        row['fdr_q'] = q.get(row['feature'])
    return result
