import numpy as np

from blade_world.fusion import GAP_S, MANEUVER, World


def pt(tid, x, y, cls="soldier_standing", grp="soldier", sigma=2.0):
    return {"tid": tid, "cls": cls, "grp": grp, "conf": 0.8, "xyz": [x, y, 50.0], "sigma_m": sigma}


def test_fusion_smooths_noise_and_tracks_velocity():
    w = World("w")
    rng = np.random.default_rng(0)
    for k in range(120):  # 4 s at 30 fps, walking east at 1.5 m/s, sigma 2 m
        t = k / 30
        w.observe("s1", [pt(1, 100 + 1.5 * t + rng.normal(0, 2), 200 + rng.normal(0, 2))], t)
    (o,) = w.objects.values()
    assert abs(o.x[0] - (100 + 1.5 * 119 / 30)) < 1.5 and abs(o.x[1] - 200) < 1.5
    assert abs(o.x[2] - 1.5) < 0.8
    assert o.sigma >= 0.5 * 2.0 - 1e-9  # floored: one camera's errors are correlated


def test_broken_track_is_stitched_in_ground_coordinates():
    w = World("w")
    for k in range(30):
        w.observe("s1", [pt(1, 100 + k * 0.05, 200)], k / 30)
    # tid 1 lost; 1.2 s later the tracker starts tid 7 where the soldier walked to
    w.observe("s1", [pt(7, 101.5 + 0.1, 200)], 1.0 + 1.2)
    assert len(w.objects) == 1 and w.stats["stitch"] == 1
    # after the stitch window a far-away new track is a new object
    w.observe("s1", [pt(9, 160, 260)], 6.0)
    assert len(w.objects) == 2


def test_two_soldiers_side_by_side_stay_two():
    w = World("w")
    for k in range(10):
        w.observe("s1", [pt(1, 100, 200), pt(2, 101.0, 200)], k / 30)
    assert len(w.objects) == 2
    # a third, new track next to them while both are still watched: a third object
    w.observe("s1", [pt(1, 100, 200), pt(2, 101.0, 200), pt(3, 100.5, 200.5)], 10 / 30)
    assert len(w.objects) == 3


def test_two_drones_one_world_merge_other_world_does_not():
    w = World("w")
    for k in range(10):
        w.observe("uav1", [pt(4, 300, 300, "tank", "tank", sigma=3)], k / 30)
        w.observe("uav2", [pt(11, 302, 299, "tank", "tank", sigma=4)], k / 30)
    assert len(w.objects) == 1 and w.stats["merge"] == 1
    (o,) = w.objects.values()
    assert sorted(o.seen_by) == ["uav1", "uav2"]
    other = World("x")
    other.observe("uav3", [pt(4, 300, 300, "tank", "tank")], 0.0)
    assert len(other.objects) == 1 and len(w.objects) == 1


def test_groups_never_merge_and_events_dedupe():
    w = World("w", confirm_n=1)
    w.observe("s1", [pt(1, 100, 100, "tank", "tank")], 0.0)
    w.observe("s2", [pt(2, 100, 100)], 0.1)
    assert len(w.objects) == 2
    _, e1 = w.observe("s1", [pt(5, 120, 120, "explosion", "explosion")], 0.2)
    _, e2 = w.observe("s1", [pt(6, 124, 121, "explosion", "explosion")], 0.5)
    assert len(e1) == 1 and e2 == []
    assert w.smoothed_counts(0.5) == {"soldier": 1.0, "tank": 1.0}


def tank(tid, x, y, cls="tank", sigma=1.2):
    return pt(tid, x, y, cls, cls, sigma=sigma)


def test_vehicle_seen_again_after_looking_away_is_the_same_object():
    w = World("w")
    for k in range(30):  # a parked tank, 3 s in view
        w.observe("s1", [tank(1, 500, 500)], k / 10)
    w.sweep(10.0)  # out of view: stale, remembered
    assert w.objects["w-0001"].state == "stale"
    w.observe("s1", [tank(14, 501.5, 499.0)], 45.0)  # 42 s later the tracker has a new ID
    assert list(w.objects) == ["w-0001"] and w.stats["reacquire"] == 1
    assert w.objects["w-0001"].state == "active"
    w.observe("s1", [tank(20, 530, 500)], 90.0)  # 30 m away from a still tank: another vehicle
    assert len(w.objects) == 2


def test_moving_vehicle_is_found_further_away():
    w = World("w")
    for k in range(40):  # driving east at 6 m/s
        w.observe("s1", [tank(1, 500 + 0.6 * k, 500)], k / 10)
    w.observe("s1", [tank(2, 523.4 + 30, 500)], 3.9 + 5.0)  # 5 s unseen, 30 m on
    assert len(w.objects) == 1 and w.stats["reacquire"] == 1


def test_tank_military_vehicle_flip_is_one_object_shown_as_the_usual_class():
    w = World("w")
    for k in range(20):
        w.observe("s1", [tank(1, 500, 500)], k / 10)
    w.observe("s1", [tank(2, 500.5, 500, cls="military_vehicle")], 2.0)  # new ID, other class
    for k in range(5):
        w.observe("s1", [tank(2, 500.5, 500, cls="military_vehicle")], 2.1 + k / 10)
    (o,) = w.objects.values()
    assert o.grp == "tank" and len(o.tracks) == 2


def test_vehicle_id_switch_from_one_frame_to_the_next_is_stitched():
    w = World("w")
    for k in range(20):
        w.observe("s1", [tank(1, 500, 500)], k / 10)
    w.observe("s1", [tank(7, 500.8, 500.3)], 2.0)  # tid 1 gone, tid 7 in the very next frame
    assert len(w.objects) == 1 and w.stats["stitch"] == 1


def test_doubled_vehicle_box_is_merged_into_the_older_object():
    w = World("w")
    for k in range(10):
        w.observe("s1", [tank(1, 500, 500)], k / 10)
    merged = []
    for k in range(4):  # the tracker keeps a second box on the same tank
        w.observe("s1", [tank(1, 500, 500), tank(2, 501.2, 500.5)], 1.0 + k / 10)
        merged += w.take_merged()
    assert list(w.objects) == ["w-0001"] and merged == []  # w-0002 was tentative, never published
    assert w.by_track[("s1", 2)] == "w-0001"


def test_soldiers_coming_back_into_view_keep_their_own_ids():
    w = World("w")
    for k in range(20):  # a squad: 1.6 m apart
        w.observe("s1", [pt(1, 100, 200), pt(2, 101.6, 200), pt(3, 100.8, 201.5)], k / 10)
    w.sweep(10.0)
    # 30 s later, new track IDs, listed in another order, each ~0.4 m off
    w.observe("s1", [pt(9, 100.9, 201.2), pt(8, 101.9, 200.1), pt(7, 100.3, 199.8)], 32.0)
    assert len(w.objects) == 3 and w.stats["reacquire"] == 3
    assert w.by_track[("s1", 7)] == w.by_track[("s1", 1)]
    assert w.by_track[("s1", 8)] == w.by_track[("s1", 2)]
    assert w.by_track[("s1", 9)] == w.by_track[("s1", 3)]


def test_two_soldiers_side_by_side_are_never_merged_even_much_later():
    w = World("w")
    for k in range(20):
        w.observe("s1", [pt(1, 100, 200), pt(2, 101.0, 200)], k / 10)
    w.sweep(10.0)
    for k in range(20):  # both back, new IDs, still side by side
        w.observe("s1", [pt(5, 100.1, 200), pt(6, 101.1, 200)], 40.0 + k / 10)
    assert len(w.objects) == 2


def test_filter_restarts_after_a_jump():
    w = World("w")
    for k in range(30):
        w.observe("s1", [pt(1, 100 + 4 * k / 30, 200, "tank", "tank", sigma=1)], k / 30)
    w.observe("s1", [pt(1, 400, 200, "tank", "tank", sigma=1)], 1.0)  # same track id, 300 m away
    (o,) = w.objects.values()
    assert abs(o.x[0] - 400) < 1 and abs(o.x[2]) < 1  # no 9000 m/s velocity left over


def test_one_frame_false_positive_never_reaches_the_map():
    w = World("w")
    for k in range(10):  # a real tank, published from the 5th frame on
        tracked = [tank(1, 500, 500)] + ([tank(2, 700, 300)] if k < 2 else [])  # a 2-frame false positive
        changed, _ = w.observe("s1", tracked, k / 10)
        assert [o.oid for o in changed] == (["w-0001"] if k >= 4 else [])
    w.sweep(1.0 + GAP_S)
    assert list(w.objects) == ["w-0001"] and w.stats["dropped"] == 1
    assert [o.oid for o in w.published()] == ["w-0001"] and w.smoothed_counts(3.1) == {"tank": 1.0}


def test_moving_vehicle_is_predicted_ahead_with_a_widening_cone():
    w = World("w")
    for k in range(40):  # driving east at 6 m/s
        w.observe("s1", [tank(1, 500 + 0.6 * k, 500)], k / 10)
    (o,) = w.objects.values()
    p30, p60 = o.model().pred
    assert (p30.dt_s, p60.dt_s) == (30.0, 60.0)
    assert abs(p30.xy[0] - (523.4 + 180)) < 5 and abs(p60.xy[0] - (523.4 + 360)) < 10
    assert abs(p30.xy[1] - 500) < 1
    assert MANEUVER * 6 * 30 <= p30.sigma_m < p60.sigma_m


def test_standing_soldier_has_no_prediction():
    w = World("w")
    rng = np.random.default_rng(1)
    for k in range(120):
        w.observe("s1", [pt(1, 100 + rng.normal(0, 2), 200 + rng.normal(0, 2))], k / 30)
    (o,) = w.objects.values()
    assert o.model().pred is None
