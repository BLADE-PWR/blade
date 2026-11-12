import numpy as np

from blade_viewer.main import ahead, cone

PRED = [{"dt_s": 30, "xy": [30.0, 0.0], "sigma_m": 3.0}, {"dt_s": 60, "xy": [60.0, 0.0], "sigma_m": 6.0}]


def test_cone_is_the_hull_of_the_two_sigma_circles():
    hull = cone({"xy": [0.0, 0.0], "sigma_m": 1.0}, PRED)
    assert np.allclose(hull.min(0), [-2.0, -12.0], atol=1e-4) and np.allclose(hull.max(0), [72.0, 12.0], atol=1e-4)


def test_predictions_in_the_past_are_hidden():
    o = {"last": 100.0, "pred": PRED}
    assert [p["dt_s"] for p in ahead(o, 120.0)] == [30, 60]
    assert [p["dt_s"] for p in ahead(o, 140.0)] == [60]
    assert ahead(o, 170.0) == [] and ahead({"last": 100.0}, 120.0) == []
