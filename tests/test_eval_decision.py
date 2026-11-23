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
    assert st.known(12) == [(1, "soldier", (5.0, 5.0)), (2, "military_vehicle", (50.0, 50.0))]  # out of view too
    st.spawn({"id": 4, "cls": "tracked_vehicle"})  # how the game names tanks
    st.gt({"frame": 13, "objs": [[4, [7, 7, 0], 0, [0, 0, 0], ""]]})
    assert st.in_view() == [(4, "tank", (7.0, 7.0))]
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


def test_a_stale_member_does_not_move_the_group():
    g = Grouper(Params(radius_m=100.0, min_count=2))
    objs = {"a": obj("a", "tank", 0, 0, vel=(4.0, 0.0)), "b": obj("b", "tank", 50, 0, vel=(8.0, 0.0), state="stale")}
    (c,) = g.update(objs, 100.0)
    assert c["vel"] == (4.0, 0.0)  # the stale one is left out


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
    objs.pop("v3")
    # v2 moves away: v4 and v5 stay together and keep the id
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
    eng.ws.groups = eng.grouper.update(eng.ws.objects, 1.0)
    await dm.send_clusters("w", eng, 1.0)
    subject, data = sent[-1]
    assert subject == "clusters.w" and data["clusters"][0]["objects"] == ["a", "b"]
    eng.ws.objects.clear()
    eng.ws.groups = eng.grouper.update(eng.ws.objects, 2.0)
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


def test_a_speed_up_after_a_turn_is_its_own_alert():
    e = Engine("w")
    x = y = 0.0
    for t, vel in [(t, (5.0, 0.0) if t < 12 else (0.0, 5.0) if t < 26 else (0.0, 15.0)) for t in range(40)]:
        x, y = x + vel[0], y + vel[1]
        e.ws.objects = {f"t{i}": obj(f"t{i}", "tank", x, y + 20.0 * i, vel=vel, last=float(t)) for i in range(3)}
        e.step(float(t))
    titles = {a.title: a.explanation for a in e.active.values() if a.rule == "group_motion"}
    assert list(titles) == ["Group of 3 speeds up"]
    assert "km/h" in titles["Group of 3 speeds up"]
    assert any(a.title == "Group of 3 changed direction" and a.explanation.startswith("The group's heading changed")
               for a in e.history)  # the turn was raised on its own, 12 s earlier


def test_group_moving_steadily_raises_no_motion_alert():
    e = Engine("w")
    for t in range(0, 30):
        e.ws.objects = {f"t{i}": obj(f"t{i}", "tank", 5.0 * t, 20.0 * i, vel=(5.0, 0.0), last=float(t))
                        for i in range(3)}
        e.step(float(t))
    assert not [a for a in e.active.values() if a.rule == "group_motion"]


def test_group_motion_ignores_members_joining_and_leaving():
    e = Engine("w")
    for t in range(0, 30):  # two tanks at 8 m/s; two standing soldiers are with them from 12 s to 24 s
        objs = [obj(f"t{i}", "tank", 8.0 * t, 20.0 * i, vel=(8.0, 0.0), last=float(t)) for i in range(2)]
        if 12 <= t < 24:
            objs += [obj(f"s{i}", "soldier", 8.0 * t, 40.0 + 20.0 * i, vel=(0.0, 0.01), last=float(t))
                     for i in range(2)]
        e.ws.objects = {o["oid"]: o for o in objs}
        e.step(float(t))
    assert not [a for a in e.active.values() if a.rule == "group_motion"]


def test_a_paused_clock_adds_no_motion_history():
    e = Engine("w")
    e.ws.objects = {f"t{i}": obj(f"t{i}", "tank", 20.0 * i, 0.0, vel=(5.0, 0.0)) for i in range(3)}
    for _ in range(20):
        e.step(100.0)
    (hist,) = e.ws.motion.values()
    assert len(hist) == 1


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


class InfoMsg:
    def __init__(self, **world):
        info = {"mission": "m1", "map": "altis", "kind": "live", "started": 0.0, "drones": {}, **world}
        self.subject, self.headers = f"worlds.{info['wid']}", None
        self.data = Envelope(wid=info["wid"], data=info).model_dump_json().encode()


async def test_rules_belong_to_the_mission_on_every_map_and_after_it_ends(monkeypatch):
    for name, value in (("engines", {}), ("missions", {}), ("rules_by_mission", {}), ("ended", set()),
                        ("sid_wid", {})):
        monkeypatch.setattr(dm, name, value)
    await dm.on_world_info(InfoMsg(wid="w-altis"))
    await dm.on_world_info(InfoMsg(wid="w-stratis", map="stratis"))
    await dm.on_world_info(InfoMsg(wid="w-replay", kind="replay"))
    for wid in ("w-altis", "w-stratis", "w-replay"):
        await dm.on_objects(ObjectsMsg(wid, {"t": 1.0}))
    await dm.put_rules("w-altis", {"lost_contact": {"enabled": False}})
    assert dm.engines["w-stratis"].rules["lost_contact"]["enabled"] is False  # the same mission on another map
    assert dm.engines["w-replay"].rules["lost_contact"].get("enabled") is not False  # a replay keeps its own
    await dm.on_world_info(InfoMsg(wid="w-altis", ended=2.0))
    assert "w-altis" not in dm.engines
    await dm.on_objects(ObjectsMsg("w-altis", {"t": 3.0}))  # still on its way when it ended
    assert "w-altis" not in dm.engines
    await dm.on_world_info(InfoMsg(wid="w-altis2"))  # the mission starts again on altis
    await dm.on_objects(ObjectsMsg("w-altis2", {"t": 4.0}))
    assert dm.engines["w-altis2"].rules["lost_contact"]["enabled"] is False


async def test_clusters_drop_forgotten_objects(monkeypatch):
    monkeypatch.setattr(dm, "engines", {})
    tanks = [obj("t1", "tank", 0, 0, last=200.0), obj("t2", "tank", 50, 0, last=200.0),
             obj("t3", "tank", 100, 0, last=200.0)]
    await dm.on_objects(ObjectsMsg("w1", {"objects": tanks, "t": 200.0}))
    e = dm.engines["w1"]
    (a,) = e.grouper.update(e.ws.objects, e.now())
    assert a["objects"] == ["t1", "t2", "t3"]
    await dm.on_objects(ObjectsMsg("w1", {"forgotten": ["t1", "t2"], "t": 200.0}))  # its replay went back
    assert e.grouper.update(e.ws.objects, e.now()) == []  # t3 is alone
