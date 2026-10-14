"""Forecasting techniques of lesson 10, implemented by hand with numpy.

Pure functions on arrays: no I/O, so they can be unit-tested.
"""

import numpy as np


def moving_average(x, window):
    """Simple (trailing) moving average.

        SMA_t = (1 / w) * sum_{i=0}^{w-1} x_{t-i}

    The first w-1 values have no full window and are NaN.
    """
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if window < 1:
        raise ValueError("window must be >= 1")
    if len(x) >= window:
        cumulative = np.cumsum(np.insert(x, 0, 0.0))
        out[window - 1:] = (cumulative[window:] - cumulative[:-window]) / window
    return out


def exponential_smoothing(x, alpha):
    """Simple exponential smoothing.

        s_0 = x_0
        s_t = alpha * x_t + (1 - alpha) * s_(t-1)        with 0 < alpha <= 1

    A larger alpha follows the data more closely, a smaller one smooths more.
    """
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    x = np.asarray(x, dtype=float)
    out = np.empty(len(x))
    for t, value in enumerate(x):
        out[t] = value if t == 0 else alpha * value + (1 - alpha) * out[t - 1]
    return out


def linear_regression(x, y):
    """Ordinary least squares line y = a + b * x, with the coefficient of determination.

        b = sum((x_i - mean(x)) * (y_i - mean(y))) / sum((x_i - mean(x))^2)
        a = mean(y) - b * mean(x)
        R^2 = 1 - SS_res / SS_tot,   SS_res = sum((y_i - a - b*x_i)^2),
                                     SS_tot = sum((y_i - mean(y))^2)

    Returns (slope b, intercept a, R^2). A constant series fitted exactly has R^2 = 1.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) != len(y) or len(x) < 2:
        raise ValueError("need at least two (x, y) pairs of the same length")
    dx = x - x.mean()
    denominator = float(np.sum(dx ** 2))
    if denominator == 0.0:
        raise ValueError("x values must not all be equal")
    slope = float(np.sum(dx * (y - y.mean())) / denominator)
    intercept = float(y.mean() - slope * x.mean())
    ss_res = float(np.sum((y - (intercept + slope * x)) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    if ss_tot == 0.0:
        r2 = 1.0 if ss_res < 1e-12 else 0.0
    else:
        r2 = 1.0 - ss_res / ss_tot
    return slope, intercept, r2
