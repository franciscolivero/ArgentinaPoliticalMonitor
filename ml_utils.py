#!/usr/bin/env python3
"""
ml_utils.py — small, dependency-light machine-learning helpers for the forecast models
(numpy only, so the GitHub Actions runners need nothing else).

  * RidgeLogit: L2-penalised logistic regression fitted by Newton–Raphson, with standardised
    features (the intercept is not penalised).
  * loo(): leave-one-out cross-validation — refits the model once per observation, predicts the
    held-out case, and returns the out-of-sample probabilities plus Brier score, log loss and
    accuracy. With a few dozen elections this is the honest way to report how good the model is.
  * choose_lambda(): picks the penalty that minimises the leave-one-out log loss.
"""

import numpy as np


class RidgeLogit:
    def __init__(self, lam=1.0, max_iter=100):
        self.lam, self.max_iter = lam, max_iter

    def fit(self, X, y):
        X = np.asarray(X, float)
        y = np.asarray(y, float)
        self.mu = X.mean(0)
        self.sd = X.std(0)
        self.sd[self.sd == 0] = 1.0
        Z = np.column_stack([np.ones(len(X)), (X - self.mu) / self.sd])
        beta = np.zeros(Z.shape[1])
        pen = np.full(Z.shape[1], self.lam)
        pen[0] = 0.0
        for _ in range(self.max_iter):
            p = 1 / (1 + np.exp(-Z @ beta))
            W = p * (1 - p)
            g = Z.T @ (y - p) - pen * beta
            H = (Z * W[:, None]).T @ Z + np.diag(pen) + 1e-9 * np.eye(len(beta))
            step = np.linalg.solve(H, g)
            beta += step
            if np.max(np.abs(step)) < 1e-8:
                break
        self.beta = beta
        return self

    def predict_proba(self, X):
        X = np.atleast_2d(np.asarray(X, float))
        Z = np.column_stack([np.ones(len(X)), (X - self.mu) / self.sd])
        return 1 / (1 + np.exp(-Z @ self.beta))

    def coef_original_scale(self):
        """Intercept and slopes on the original (unstandardised) feature scale."""
        b = self.beta[1:] / self.sd
        a = self.beta[0] - np.sum(b * self.mu)
        return float(a), [float(x) for x in b]


def loo(X, y, lam):
    X = np.asarray(X, float)
    y = np.asarray(y, float)
    out = np.zeros(len(y))
    for i in range(len(y)):
        m = np.ones(len(y), bool)
        m[i] = False
        out[i] = RidgeLogit(lam).fit(X[m], y[m]).predict_proba(X[i:i + 1])[0]
    eps = 1e-6
    brier = float(np.mean((out - y) ** 2))
    logloss = float(-np.mean(y * np.log(out + eps) + (1 - y) * np.log(1 - out + eps)))
    acc = float(np.mean((out > 0.5) == (y == 1)))
    base = float(np.mean((y.mean() - y) ** 2))        # Brier of always predicting the base rate
    return {"p": out.tolist(), "brier": brier, "logloss": logloss, "accuracy": acc, "brier_baseline": base}


def choose_lambda(X, y, grid=(0.1, 0.3, 1, 3, 10, 30)):
    scores = {lam: loo(X, y, lam)["logloss"] for lam in grid}
    return min(scores, key=scores.get), scores
