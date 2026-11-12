import numpy as np

from blade_viewer.main import cone


def test_cone_is_the_hull_of_the_two_sigma_circles():
    o = {"xy": [0.0, 0.0], "sigma_m": 1.0,
         "pred": [{"dt_s": 30, "xy": [30.0, 0.0], "sigma_m": 3.0}, {"dt_s": 60, "xy": [60.0, 0.0], "sigma_m": 6.0}]}
    hull = cone(o)
    assert np.allclose(hull.min(0), [-2.0, -12.0], atol=1e-4) and np.allclose(hull.max(0), [72.0, 12.0], atol=1e-4)
