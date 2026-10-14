import numpy as np
import pytest

from src.forecasting import exponential_smoothing, linear_regression, moving_average


def test_moving_average_is_trailing_and_nan_before_the_first_full_window():
    out = moving_average([1, 2, 3, 4, 5], 3)
    assert np.isnan(out[:2]).all()
    assert out[2:] == pytest.approx([2.0, 3.0, 4.0])


def test_moving_average_shorter_than_window_is_all_nan():
    assert np.isnan(moving_average([1, 2], 3)).all()


def test_exponential_smoothing_follows_the_recurrence():
    x = [10.0, 20.0, 30.0]
    s = exponential_smoothing(x, 0.5)
    assert s[0] == 10.0
    assert s[1] == pytest.approx(0.5 * 20 + 0.5 * 10)
    assert s[2] == pytest.approx(0.5 * 30 + 0.5 * s[1])


def test_exponential_smoothing_alpha_one_is_the_series_itself():
    assert exponential_smoothing([3, 1, 4], 1.0) == pytest.approx([3, 1, 4])


def test_exponential_smoothing_rejects_a_bad_alpha():
    with pytest.raises(ValueError):
        exponential_smoothing([1, 2], 0)


def test_linear_regression_exact_line():
    slope, intercept, r2 = linear_regression([0, 1, 2, 3], [5, 3, 1, -1])
    assert slope == pytest.approx(-2.0)
    assert intercept == pytest.approx(5.0)
    assert r2 == pytest.approx(1.0)


def test_linear_regression_noisy_data_has_r2_below_one_and_known_value():
    # hand computed: x = 0..4, y = [1, 3, 2, 5, 4] -> b = 8/10 = 0.8, a = 3 - 0.8*2 = 1.4, SS_res = 3.6, SS_tot = 10
    slope, intercept, r2 = linear_regression([0, 1, 2, 3, 4], [1, 3, 2, 5, 4])
    assert slope == pytest.approx(0.8)
    assert intercept == pytest.approx(1.4)
    assert r2 == pytest.approx(1 - 3.6 / 10)


def test_linear_regression_constant_series():
    slope, _, r2 = linear_regression([0, 1, 2], [7, 7, 7])
    assert slope == pytest.approx(0.0)
    assert r2 == 1.0


def test_linear_regression_needs_two_points():
    with pytest.raises(ValueError):
        linear_regression([1], [1])
