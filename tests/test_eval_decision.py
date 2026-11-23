import blade_decision.main as dm
from blade_common.contracts import Envelope
from blade_decision.clusters import Grouper, Params, area, outline
from blade_decision.main import Engine
from blade_evaluator.evaluate import StreamTruth, WorldEval, cep, inside, match


def obj(oid, grp, x, y, n=50, sigma=2.0, state="active", last=100.0, vel=(0.0, 0.0)):
    return {"oid": oid, "grp": grp, "xy": [x, y], "n_obs": n, "sigma_m": sigma, "state": state, "last": last,
            "vel": list(vel)}


def test_match_is_greedy_within_gate_and_group():
    truth = [(1, "soldier", (0, 0)), (2, "soldier", (5, 0)), (3, "tank", (100, 100))]
    est = [("a", "soldier", (4.5, 0.2)), ("b", "soldier", (0.3, 0)), ("c", "soldier", (100, 100)),
           ("d", "tank", (130, 100))]
    pairs, lost_t, extra_e = match(truth, est)
    assert sorted((t, e) for t, e, _ in pairs) == [(1, "b"), (2, "a")]
    assert lost_t == {2} and extra_e == {2, 3}  # truth index 2 (tank) unmatched: 30 m > gate


def test_truth_in_view_and_cep():
    st = StreamTruth()
    st.spawn({"id": 1, "cls": "soldier"})
    st.spawn({"id": 2, "cls": "wheeled_vehicle"})
    st.spawn({"id": 3, "cls": "civilian_car"})
    st.gt({"frame": 10, "objs": [[1, [5, 5, 0], 0, [0, 0, 0], "STAND"], [2, [50, 50, 0], 0, [0, 0, 0], ""],
                                 [3, [6, 6, 0], 0, [0, 0, 0], ""]]})
    st.footprint = [[0, 0], [20, 0], [20, 20], [0, 20]]
    assert st.in_view(12) == [(1, "soldier", (5.0, 5.0))]
    assert st.in_view(9) == []  # no truth at or before frame 9
    assert inside(st.footprint, 10, 10) and not inside(st.footprint, 30, 10)
    assert cep([1, 2, 3, 4, 100]) == (3.0, 61.6)
    ev = WorldEval("w")
    ev.add("world", "soldier", 1.0, 0.0)
    ev.add("world", "soldier", 3.0, 50.0)
    ev.trim(50.0)
    assert ev.summary()["soldier"]["n"] == 1 and ev.report()["world"]["soldier"]["n"] == 2


def test_vehicle_group_raises_and_clears():
    e = Engine("w")
    e.ws.objects = {o["oid"]: o for o in [obj("v1", "tank", 0, 0), obj("v2", "tank", 150, 0),
                                          obj("v3", "military_vehicle", 300, 0), obj("s1", "soldier", 10, 10)]}
    (a,) = [x for x in e.step(100.0) if x.rule == "vehicle_group"]
    assert a.objects == ["v1", "v2", "v3"] and a.state == "active"
    assert not [x for x in e.step(101.0) if x.rule == "vehicle_group"]  # still active: nothing new
    e.ws.objects.pop("v3")
    assert not [x for x in e.step(102.0) if x.rule == "vehicle_group"]  # held for a few seconds
    (c,) = [x for x in e.step(108.0) if x.rule == "vehicle_group"]
    assert c.aid == a.aid and c.state == "cleared"


def test_lost_contact_zone_and_resolution_rules():
    e = Engine("w")
    e.rules["zone"]["zones"] = [{"name": "bridge", "polygon": [[100, -10], [120, -10], [120, 10], [100, 10]]}]
    e.ws.objects = {"t1": obj("t1", "tank", 0, 0, vel=(4.0, 0.0), last=200.0),
                    "s1": obj("s1", "soldier", 110, 0, last=200.0)}
    e.ws.gone = {"t9": obj("t9", "tank", 500, 500, last=100.0)}
    e.ws.cameras = {"uav1": {"gsd_m": 0.09, "centre": [0, 0, 0], "t": 200.0}}
    rules = {(a.rule, tuple(a.objects)) for a in e.step(200.0)}
    assert ("lost_contact", ("t9",)) in rules
    assert ("zone", ("s1",)) in rules and ("zone", ("t1",)) in rules  # inside / heading in (~25 s)
    assert ("infantry_resolution", ()) in rules


def test_group_alert_survives_membership_change():
    e = Engine("w")
    e.ws.objects = {o["oid"]: o for o in [obj("v1", "tank", 0, 0), obj("v2", "tank", 50, 0), obj("v3", "tank", 90, 0)]}
    (a,) = [x for x in e.step(1.0) if x.rule == "vehicle_group"]
    e.ws.objects.pop("v1")  # smallest id leaves, two new ones join
    e.ws.objects.update({o["oid"]: o for o in [obj("v4", "tank", 120, 0), obj("v5", "tank", 140, 0)]})
    assert [x for x in e.step(2.0) if x.rule == "vehicle_group"] == []  # updated in place, no raise/clear
    (live,) = [x for x in e.active.values() if x.rule == "vehicle_group"]
    assert live.aid == a.aid and live.objects == ["v2", "v3", "v4", "v5"]


def test_clusters_group_every_class_and_describe_them():
    g = Grouper(Params(radius_m=100.0, min_count=2, max_age_s=60.0))
    objs = {o["oid"]: o for o in [obj("t1", "tank", 0, 0, vel=(4.0, 0.0)), obj("s1", "soldier", 80, 0, vel=(0.0, 2.0)),
                                  obj("s2", "soldier", 160, 0), obj("far", "soldier", 900, 900)]}
    (c,) = g.update(objs, 100.0)  # t1 - s1 - s2 are linked through s1; the far one is alone
    assert c["objects"] == ["s1", "s2", "t1"] and c["composition"] == {"tank": 1, "soldier": 2}
    assert c["centre"] == (80.0, 0.0) and c["vel"] == (1.33, 0.67)  # mean of three, the third stands still
    assert c["area_m2"] > area(outline([(0.0, 0.0)]))  # more than one object's outline


def test_clusters_keep_stale_objects_for_a_while_and_one_object_has_an_outline():
    g = Grouper(Params(radius_m=100.0, min_count=2, max_age_s=30.0))
    objs = {"a": obj("a", "tank", 0, 0, last=100.0), "b": obj("b", "tank", 50, 0, state="stale", last=90.0)}
    assert len(g.update(objs, 100.0)) == 1  # b was seen 10 s ago
    assert g.update(objs, 125.0) == []  # 35 s ago: out
    assert len(outline([(5.0, 5.0)])) >= 3 and area(outline([(5.0, 5.0)])) > 1000


def test_cluster_id_survives_membership_change_and_a_split_gets_a_new_one():
    g = Grouper(Params(radius_m=100.0, min_count=2))
    objs = {o["oid"]: o for o in [obj("v1", "tank", 0, 0), obj("v2", "tank", 50, 0), obj("v3", "tank", 90, 0)]}
    (a,) = g.update(objs, 1.0)
    objs.pop("v1")
    objs.update({o["oid"]: o for o in [obj("v4", "tank", 120, 0), obj("v5", "tank", 140, 0)]})
    (b,) = g.update(objs, 2.0)
    assert b["cid"] == a["cid"] and b["objects"] == ["v2", "v3", "v4", "v5"]
    objs.pop("v3")  # v2 | v4 v5 are 70 m apart: still one; move v2 away to split off
    objs["v2"]["xy"] = [-500.0, 0.0]
    (c,) = g.update(objs, 3.0)
    assert c["objects"] == ["v4", "v5"] and c["cid"] == a["cid"]


async def test_clusters_are_published_and_cleared_when_the_last_goes(monkeypatch):
    sent = []

    async def publish(nc, subject, env):
        sent.append((subject, env.data))

    monkeypatch.setattr(dm, "publish", publish)
    dm.state["nc"] = object()
    dm.engines.pop("w", None)
    eng = dm.engine("w")
    eng.ws.objects = {o["oid"]: o for o in [obj("a", "tank", 0, 0), obj("b", "soldier", 40, 0)]}
    eng.clusters = eng.grouper.update(eng.ws.objects, 1.0)
    await dm.send_clusters("w", eng, 1.0)
    subject, data = sent[-1]
    assert subject == "clusters.w" and data["clusters"][0]["objects"] == ["a", "b"]
    eng.ws.objects.clear()
    eng.clusters = eng.grouper.update(eng.ws.objects, 2.0)
    await dm.send_clusters("w", eng, 2.0)
    assert sent[-1][1]["clusters"] == []


def test_group_motion_alerts_on_a_turn_and_on_speeding_up():
    e = Engine("w")
    for t in range(0, 12):  # three tanks go east at 3 m/s
        e.ws.objects = {f"t{i}": obj(f"t{i}", "tank", 3.0 * t, 20.0 * i, vel=(3.0, 0.0), last=float(t))
                        for i in range(3)}
        assert not [x for x in e.step(float(t)) if x.rule == "group_motion"]
    for t in range(12, 24):  # then north at 9 m/s: a turn of 90 degrees and three times the speed
        e.ws.objects = {f"t{i}": obj(f"t{i}", "tank", 36.0, 20.0 * i + 9.0 * (t - 12), vel=(0.0, 9.0), last=float(t))
                        for i in range(3)}
        e.step(float(t))
    rules = {a.title for a in e.active.values() if a.rule == "group_motion"}
    assert rules == {"Group of 3 changed direction", "Group of 3 speeds up"}


def test_group_moving_steadily_raises_no_motion_alert():
    e = Engine("w")
    for t in range(0, 30):
        e.ws.objects = {f"t{i}": obj(f"t{i}", "tank", 5.0 * t, 20.0 * i, vel=(5.0, 0.0), last=float(t))
                        for i in range(3)}
        e.step(float(t))
    assert not [a for a in e.active.values() if a.rule == "group_motion"]


class ObjectsMsg:
    def __init__(self, wid, data):
        self.subject, self.headers = f"world.{wid}.objects", None
        self.data = Envelope(wid=wid, data={"map": "tiny", **data}).model_dump_json().encode()


async def test_paused_replay_raises_no_lost_contact(monkeypatch):
    monkeypatch.setattr(dm, "engines", {})
    await dm.on_objects(ObjectsMsg("w1", {"objects": [obj("t1", "tank", 0, 0, last=200.0)], "t": 200.0}))
    e = dm.engines["w1"]
    assert e.now() == 200.0  # the world's clock, not the wall's
    assert not [a for a in e.step(e.now()) if a.rule == "lost_contact"]  # paused: world time stands at 200
    await dm.on_objects(ObjectsMsg("w1", {"t": 261.0}))  # 61 s of world time later, still not seen
    assert [a.objects for a in e.step(e.now()) if a.rule == "lost_contact"] == [["t1"]]


async def test_forgotten_objects_are_not_lost(monkeypatch):
    monkeypatch.setattr(dm, "engines", {})
    await dm.on_objects(ObjectsMsg("w1", {"objects": [obj("t1", "tank", 0, 0, last=200.0)], "t": 200.0}))
    await dm.on_objects(ObjectsMsg("w1", {"forgotten": ["t1"], "t": 300.0}))  # its replay went back
    e = dm.engines["w1"]
    assert "t1" not in e.ws.objects and "t1" not in e.ws.gone
    assert not [a for a in e.step(e.now()) if a.rule == "lost_contact"]


async def test_new_map_clears_the_worlds_alerts_and_objects(monkeypatch):
    monkeypatch.setattr(dm, "engines", {})
    sent = []

    async def publish(nc, subject, env):
        sent.append((subject, env.data["alerts"]))

    monkeypatch.setattr(dm, "publish", publish)
    await dm.on_objects(ObjectsMsg("w1", {"objects": [obj("t1", "tank", 0, 0, last=200.0)], "t": 200.0}))
    await dm.on_objects(ObjectsMsg("w1", {"expired": ["t1"], "t": 261.0}))
    e = dm.engines["w1"]
    (lost,) = [a for a in e.step(e.now()) if a.rule == "lost_contact"]
    await dm.on_objects(ObjectsMsg("w1", {"map": "other", "t": 262.0}))
    assert not e.active and not e.ws.objects and not e.ws.gone and e.map == "other"
    assert [(s, [(a["aid"], a["state"]) for a in alerts]) for s, alerts in sent] == [
        ("alerts.w1", [(lost.aid, "cleared")])]


async def test_eval_leaves_out_drones_on_another_map(monkeypatch):
    import blade_evaluator.main as em

    ev = em.Evaluator()
    sent = []

    async def publish(nc, subject, env):
        sent.append(env.data)

    monkeypatch.setattr(em, "publish", publish)
    for sid, map_name, x in (("uav1", "altis", 5), ("uav2", "stratis", 105)):
        st = ev.truth[sid]
        st.spawn({"id": x, "cls": "soldier"})
        st.gt({"frame": 10, "objs": [[x, [x, x, 0], 0, [0, 0, 0], "STAND"]]})
        st.footprint = [[x - 5, x - 5], [x + 5, x - 5], [x + 5, x + 5], [x - 5, x + 5]]
        ev.sid_wid[sid], ev.sid_map[sid] = "m", map_name
    ev.world_map["m"] = "altis"
    await ev.evaluate("m", 0.0)
    assert [x["truth_id"] for x in sent[-1]["truth"]] == [5]  # only the altis drone's soldier


async def test_rewind_clears_the_alerts_of_forgotten_objects_at_once(monkeypatch):
    monkeypatch.setattr(dm, "engines", {})
    sent = []

    async def publish(nc, subject, env):
        sent.append(env.data["alerts"])

    monkeypatch.setattr(dm, "publish", publish)
    tanks = [obj("t1", "tank", 0, 0, last=200.0), obj("t2", "tank", 500, 0, last=200.0)]
    await dm.on_objects(ObjectsMsg("w1", {"objects": tanks, "t": 261.0}))
    e = dm.engines["w1"]
    lost = {a.objects[0]: a for a in e.step(e.now()) if a.rule == "lost_contact"}
    assert sorted(lost) == ["t1", "t2"]
    await dm.on_objects(ObjectsMsg("w1", {"forgotten": ["t1"], "t": 261.0}))  # its replay looped
    assert sent == [[{**lost["t1"].model_dump(exclude_none=True), "state": "cleared", "t": 261.0}]]
    assert [a.objects for a in e.active.values()] == [["t2"]]  # seen by a stream that did not go back
