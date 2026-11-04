#!/usr/bin/env python3
"""Rule checks for lantern. Run: python3 tests/test_lantern.py [test_name ...]"""
import json
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))
import lantern as D  # noqa: E402

HERE = BIN
OPTS = [{"label": x} for x in ("A", "B", "C")]


def refused(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except D.Refused as e:
        return str(e)
    raise AssertionError(f"{fn.__name__} was not refused")


def chart():
    """Round 2, user's turn. Goal n1; questions n2, n3; task n4. All locked, all in review."""
    m = D.new_map("t", "Goal")
    D.add_node(m, "agent", "question", "Q1", parent_ids=["n1"], options=OPTS, body="why")
    D.add_node(m, "agent", "question", "Q2", parent_ids=["n1"], options=OPTS, body="why")
    D.add_node(m, "agent", "task", "T", parent_ids=["n1"])
    D.close_round(m)
    return m


def test_types_and_parents_are_checked():
    m = D.new_map("t", "G")
    assert "3 or 4 options" in refused(D.add_node, m, "agent", "question", "Q", parent_ids=["n1"], options=OPTS[:2], body="b")
    assert "needs a body" in refused(D.add_node, m, "agent", "question", "Q", parent_ids=["n1"], options=OPTS)
    assert "exactly one goal" in refused(D.add_node, m, "agent", "goal", "G2")
    assert "needs a parent" in refused(D.add_node, m, "agent", "task", "T")
    assert "title" in refused(D.add_node, m, "agent", "task", "  ", parent_ids=["n1"])
    m = chart()
    assert "cannot create" in refused(D.add_node, m, "user", "decision", "x", parent_ids=["n1"])
    assert "still open" in refused(D.add_node, m, "agent", "task", "early", parent_ids=["n1"])


def test_locked_items_cannot_change():
    m = chart()
    assert "locked" in refused(D.edit_node, m, "n2", "new", "")
    assert "locked" in refused(D.remove_node, m, "n2")
    r = D.add_node(m, "user", "comment", "R", parent_ids=["n4"])
    D.add_edge(m, "user", r, "n3")  # the user may link into a closed round
    D.add_edge(m, "user", "n2", r)
    assert set(D.parents(m, r)) == {"n4", "n2"}
    assert "locked" in refused(D.remove_edge, m, "n1", "n2")
    D.edit_node(m, r, "R2", "body")
    assert D.node(m, r)["title"] == "R2"


def test_cycles_and_orphans_are_refused():
    m = chart()
    r = D.add_node(m, "user", "comment", "R", parent_ids=["n2"])
    q = D.add_node(m, "user", "comment", "Q", parent_ids=[r])
    assert "loop" in refused(D.add_edge, m, "user", q, r)
    assert "itself" in refused(D.add_edge, m, "user", q, q)
    assert "Already linked" in refused(D.add_edge, m, "user", r, q)
    assert "last parent" in refused(D.remove_edge, m, r, q)
    assert "replies" in refused(D.remove_node, m, r)
    D.remove_node(m, q)
    D.remove_node(m, r)
    assert [n["id"] for n in m["nodes"]] == ["n1", "n2", "n3", "n4"]


def test_unreviewed_nodes_wait_for_the_user():
    m = chart()
    a = D.answer(m, "n2", choice=1, comment="because")
    c = D.add_node(m, "user", "comment", "Not sure", parent_ids=["n3"])
    D.mark_unclear(m, "n3")
    D.end_turn(m)  # n1 and n4 stay unreviewed: the user went deep on n2 and n3
    D.add_node(m, "agent", "decision", "D", parent_ids=[a])
    D.add_node(m, "agent", "finding", "F", parent_ids=[c])
    assert "Only the user" in refused(D.add_edge, m, "agent", a, "n4")
    D.rewrite(m, "n3", "Q2 again", "Clearer.", options=OPTS)
    D.close_round(m)
    q = lambda: sorted(n["id"] for n in D.review_queue(m))
    assert q() == ["n1", "n3", "n4", "n7", "n8"]  # n2 is answered; n3 was rewritten, so it is back
    D.approve(m, "n4")
    assert "n4" in q()  # approved this round: still shown, still undoable
    D.end_turn(m)
    D.close_round(m)
    assert q() == ["n1", "n3", "n7", "n8"]
    assert "already reviewed" in refused(D.approve, m, "n4")


def test_end_turn_and_unlock():
    m = chart()
    D.answer(m, "n3", text="Something else")
    assert "Answer a question" in refused(D.approve, m, "n2")
    D.approve(m, "n4")
    D.unapprove(m, "n4")
    D.end_turn(m)
    assert m["ended"]
    assert "ended" in refused(D.add_node, m, "user", "comment", "late", parent_ids=["n1"])
    assert "ended" in refused(D.approve, m, "n1")
    D.unlock(m)
    D.add_node(m, "user", "comment", "late", parent_ids=["n1"])


def test_answers_record_choice_and_comment():
    m = chart()
    a = D.node(m, D.answer(m, "n2", choice=1, comment="k3s"))
    assert (a["title"], a["choice"], a["body"], a["author"]) == ("B", 1, "k3s", "user")
    assert "already answered" in refused(D.answer, m, "n2", choice=0)
    assert "No such option" in refused(D.answer, m, "n3", choice=7)
    assert "not a question" in refused(D.answer, m, "n4", text="x")
    assert "title" in refused(D.answer, m, "n3", text=" ")


def test_turns_alternate_and_close_round_checks_replies():
    m = chart()
    a = D.answer(m, "n2", choice=0)
    b = D.answer(m, "n3", choice=2)
    r = D.add_node(m, "user", "comment", "R", parent_ids=["n4"])
    D.approve(m, "n1")
    D.end_turn(m)
    msg = refused(D.close_round, m)
    assert all(i in msg for i in (a, b, r))
    assert "agenda" in refused(D.ack, m, "n4")
    d = D.add_node(m, "agent", "decision", "Both settled", parent_ids=[a, b])
    D.ack(m, r)
    D.close_round(m)
    assert m["round"] == 3 and not m["ended"]
    assert [n["id"] for n in D.review_queue(m)] == [d]
    assert all(D.is_locked(m, D.node(m, i)) for i in (a, b, r, d))


def test_unclear_nodes_are_rewritten_in_place():
    m = chart()
    D.approve(m, "n4")
    D.mark_unclear(m, "n4")
    assert not D.has(m, "approved", "n4")
    D.mark_unclear(m, "n2")
    assert "yours" in refused(D.mark_unclear, m, D.add_node(m, "user", "comment", "C", parent_ids=["n1"]))
    D.approve(m, "n1")
    D.answer(m, "n3", choice=0)
    D.end_turn(m)  # unclear counts as handled
    D.ack(m, D.agenda(m)[0]["id"])
    D.ack(m, D.agenda(m)[1]["id"])
    assert "n4" in refused(D.close_round, m)
    assert "3 or 4" in refused(D.rewrite, m, "n2", "Q1 clearer", "why", [{"label": "A"}])
    D.rewrite(m, "n2", "Q1 clearer", "why", OPTS)
    D.rewrite(m, "n4", "T clearer", "how")
    assert "not marked" in refused(D.rewrite, m, "n4", "again", "")
    D.close_round(m)
    t = D.node(m, "n4")
    assert (t["title"], t["history"][0]["title"]) == ("T clearer", "T")
    assert {"n2", "n4"} <= {n["id"] for n in D.review_queue(m)}
    assert D.parents(m, "n4") == ["n1"]
    D.retract_turn(m)
    assert D.node(m, "n4")["title"] == "T" and D.has(m, "unclear", "n4") and "n4" in {n["id"] for n in m["nodes"]}


def test_to_fog_rewrites_a_node_as_fog():
    m = chart()
    assert "cannot become fog" in refused(D.mark_unclear, m, "n1", True)
    D.mark_unclear(m, "n2", fog=True)
    assert D.has(m, "unclear", "n2") and D.has(m, "to_fog", "n2")
    D.mark_unclear(m, "n3", fog=True)
    D.mark_unclear(m, "n3")  # back to a plain unclear mark
    assert not D.has(m, "to_fog", "n3")
    D.approve(m, "n1")
    D.approve(m, "n4")
    D.end_turn(m)
    assert "REWRITE AS FOG" in D.render_agenda(m)
    D.rewrite(m, "n2", "Fog Q1", "dim view")
    D.rewrite(m, "n3", "Q2 clearer", "why", OPTS)
    D.close_round(m)
    f = D.node(m, "n2")
    assert (f["type"], "options" in f) == ("fog", False)
    assert f["id"] in {n["id"] for n in D.frontier(m)}
    D.retract_turn(m)
    q = D.node(m, "n2")
    assert (q["type"], q["title"], len(q["options"])) == ("question", "Q1", len(OPTS)) and D.has(m, "to_fog", "n2")


def test_retract_turn_replays_the_agents_last_round():
    m = chart()
    a = D.answer(m, "n2", choice=0)
    D.answer(m, "n3", choice=1)
    D.approve(m, "n1")
    D.approve(m, "n4")
    D.end_turn(m)
    D.ack(m, D.agenda(m)[1]["id"])
    d = D.add_node(m, "agent", "decision", "D", parent_ids=[a])
    D.add_edge(m, "agent", "n3", d)
    D.add_edge(m, "agent", "n1", a)
    D.close_round(m)
    r = D.add_node(m, "user", "comment", "R", parent_ids=[d])
    assert "already worked" in refused(D.retract_turn, m)
    D.remove_node(m, r)
    D.retract_turn(m)
    assert (m["round"], m["ended"]) == (2, True)
    assert d not in {n["id"] for n in m["nodes"]} and D.parents(m, a) == ["n2"]
    assert D.agenda(m)[0]["id"] == a and not D.marked(m, "acked")
    assert "agenda" not in refused(D.close_round, m)


def test_retract_restores_the_map_from_before_the_agents_turn():
    m = chart()
    D.retract_turn(m)  # round 1: the charted nodes go, the goal stays
    assert (m["round"], m["ended"], [n["id"] for n in m["nodes"]]) == (1, True, ["n1"])
    t = D.add_node(m, "agent", "task", "T again", parent_ids=["n1"])
    assert t == "n5"  # retracted ids are not handed out again
    D.close_round(m)
    D.retract_turn(m)  # the replayed turn can be taken back too
    assert [n["id"] for n in m["nodes"]] == ["n1"]
    D.add_node(m, "agent", "task", "T", parent_ids=["n1"])
    D.close_round(m)
    D.mark_unclear(m, "n6", fog=True)  # an unclear or to-fog mark is work too
    assert "already worked" in refused(D.retract_turn, m)
    D.unmark_unclear(m, "n6")
    D.retract_turn(m)
    del m["before"]
    D.add_node(m, "agent", "task", "T", parent_ids=["n1"])
    D.close_round(m)
    assert "snapshots" in refused(D.retract_turn, m)


def test_run_picks_the_author_from_the_front_door():
    m = chart()
    c = D.run(m, "user", "add", {"type": "comment", "title": "C", "parents": ["n1"]})
    D.run(m, "user", "link", {"from": "n2", "to": c})
    assert D.node(m, c)["author"] == "user" and m["edges"][-1]["by"] == "user"
    assert "cannot create" in refused(D.run, m, "user", "add", {"type": "decision", "title": "x", "parents": ["n1"]})
    assert "Unknown op" in refused(D.run, m, "user", "close-round", {})
    D.run(m, "user", "approve", {"id": "n1"})
    D.run(m, "user", "approve", {"id": "n4"})
    D.run(m, "user", "answer", {"id": "n2", "choice": 0})
    D.run(m, "user", "answer", {"id": "n3", "choice": 0})
    D.run(m, "user", "end_turn", {})
    d = D.run(m, None, "add", {"type": "decision", "title": "D", "parents": [c]})
    D.run(m, None, "link", {"from": "n4", "to": d})
    assert D.node(m, d)["author"] == "agent" and m["edges"][-1]["by"] == "agent"


def test_pending_merges_always_fold_cleanly():
    m = chart()
    r = D.add_node(m, "user", "comment", "R", parent_ids=["n4"])
    D.request_merge(m, ["n4", r])
    c = D.add_node(m, "user", "comment", "C", parent_ids=["n4"])
    assert "pending merge" in refused(D.add_edge, m, "user", c, r)  # C would sit between n4 and R
    assert not D.reaches(m, c, r)
    s = D.add_node(m, "user", "comment", "S", parent_ids=["n1"])
    D.add_edge(m, "user", s, r)  # R has a second parent now
    assert "cut a pending merge" in refused(D.remove_edge, m, "n4", r)
    # two merges, each fine alone, that would fold into x1 -> x2 -> x1
    m = chart()
    a = D.add_node(m, "user", "comment", "A", parent_ids=["n2"])
    b = D.add_node(m, "user", "comment", "B", parent_ids=[a])
    d = D.add_node(m, "user", "comment", "D", parent_ids=["n3"])
    c = D.add_node(m, "user", "comment", "C", parent_ids=[d])
    D.add_edge(m, "user", a, c)
    D.add_edge(m, "user", d, b)
    D.request_merge(m, [a, b])
    assert "other pending merges" in refused(D.request_merge, m, [c, d])


def test_answers_follow_one_set_of_rules():
    m = chart()
    D.mark_unclear(m, "n2")
    assert "marked unclear" in refused(D.answer, m, "n2", choice=0)
    r = D.add_node(m, "user", "comment", "R", parent_ids=["n3"])
    D.request_merge(m, ["n3", r])
    assert "pending merge" in refused(D.answer, m, "n3", choice=0)
    a = D.add_node(m, "user", "answer", "done", parent_ids=["n4"])  # answering a task
    assert "already answered" in refused(D.add_node, m, "user", "answer", "again", parent_ids=["n4"])
    assert "exactly one" in refused(D.add_node, m, "user", "answer", "x", parent_ids=["n1"])
    assert "Answer this one" in refused(D.add_edge, m, "user", "n2", a)
    assert "already answered" in refused(D.mark_unclear, m, "n4", True)


def test_editing_a_picked_answer_makes_it_your_own():
    m = chart()
    a = D.answer(m, "n2", choice=1)
    D.edit_node(m, a, "B", "same title, new comment")
    assert D.node(m, a)["choice"] == 1
    D.edit_node(m, a, "Something else", "")
    assert "choice" not in D.node(m, a)


def test_only_nodes_in_review_can_be_marked_unclear():
    m = chart()
    for i in ("n1", "n4"):
        D.approve(m, i)
    D.answer(m, "n2", choice=0)
    D.answer(m, "n3", choice=0)
    D.end_turn(m)
    for n in D.agenda(m):
        D.ack(m, n["id"])
    D.add_node(m, "agent", "finding", "F", parent_ids=["n1"])
    D.close_round(m)  # round 3: the round-1 nodes are reviewed now
    assert "already reviewed" in refused(D.mark_unclear, m, "n4", True)
    assert "already reviewed" in refused(D.mark_unclear, m, "n1")


def test_requests_are_answered_with_nodes_under_them():
    m = chart()
    c = D.add_node(m, "user", "comment", "Mine", parent_ids=["n1"])
    assert "Only agent nodes" in refused(D.request, m, c, "decompose")
    assert "Only agent nodes" in refused(D.request, m, c, "diverge")
    for k in ("challenge", "research", "example"):
        D.request(m, c, k)
        D.unrequest(m, c, k)
    D.request(m, c, "reflect")
    D.request(m, "n4", "decompose")
    D.request(m, "n4", "reflect")
    D.request(m, "n2", "diverge")
    assert "has a request" in refused(D.mark_unclear, m, "n2")
    c2 = D.add_node(m, "user", "comment", "On Q1", parent_ids=["n2"])
    assert "has a request" in refused(D.request_merge, m, ["n2", c2])
    assert "Unknown request" in refused(D.request, m, "n3", "nope")
    D.unrequest(m, "n2", "diverge")
    assert "no diverge request" in refused(D.unrequest, m, "n2", "diverge")
    D.request(m, "n2", "diverge")
    D.end_turn(m)
    assert "REQUESTS" in D.render_agenda(m)
    D.add_node(m, "agent", "finding", "Reply", parent_ids=[c, c2])
    assert "decompose n4, diverge n2, reflect n4" in refused(D.close_round, m)
    D.add_node(m, "agent", "task", "Part 1", parent_ids=["n4"])
    D.add_node(m, "agent", "question", "Way A?", "Body.", parent_ids=["n2"], options=OPTS)
    D.close_round(m)
    assert "n4" not in [n["id"] for n in D.review_queue(m)]  # a request counts as a review


def test_focus_parks_open_nodes_elsewhere():
    m = chart()  # goal n1; questions n2, n3; task n4
    c = D.add_node(m, "user", "comment", "On Q2", parent_ids=["n3"])
    assert "No node" in refused(D.focus_on, m, "n99")
    D.focus_on(m, "n2")
    assert m["ended"]  # focusing ends the turn
    assert D.parked(m) == {"n3", "n4", c}  # open nodes off the focus, and what hangs only on them
    q = D.add_node(m, "agent", "question", "Deeper?", "Body.", parent_ids=["n2"], options=OPTS)
    both = D.add_node(m, "agent", "finding", "Shared", parent_ids=["n2", "n3"])
    D.add_node(m, "agent", "finding", "Reply", parent_ids=[c])
    D.close_round(m)
    assert q not in D.parked(m) and both not in D.parked(m)  # new nodes in the focus join it
    D.answer(m, "n3", choice=0)  # n3 is settled now, so it shows again: settled nodes stay visible
    assert D.parked(m) == {"n4"}
    ag, out = D.render_agenda(m), D.render_outline(m)
    assert "FOCUS: n2" in ag and "Q2" in ag and "n4 task" not in ag
    assert "Deeper?" in out and "n4 task" not in out and "1 parked nodes hidden" in out
    D.unfocus(m)
    assert D.parked(m) == set() and "no focus" in refused(D.unfocus, m)
    D.focus_on(m, "n1")  # focusing the goal clears the focus
    assert D.focus(m) is None


def test_unlock_and_retract_keep_the_right_snapshot():
    m = chart()
    D.approve(m, "n1")
    D.approve(m, "n4")
    a = D.answer(m, "n2", choice=0)
    b = D.answer(m, "n3", choice=0)
    D.end_turn(m)
    D.unlock(m)  # nothing played yet: fine
    D.end_turn(m)
    D.ack(m, a)
    assert "started its turn" in refused(D.unlock, m)
    del m["acked"][a]
    D.unlock(m)
    for x in (a, b):
        D.remove_node(m, x)
    for x in ("n1", "n4"):
        D.unapprove(m, x)
    D.retract_turn(m)  # takes back round 1, not the round-2 End turn
    assert (m["round"], [n["id"] for n in m["nodes"]]) == (1, ["n1"])


def test_merge_into_hands_over_links_with_an_author():
    m = chart()
    r = D.add_node(m, "user", "comment", "R", parent_ids=["n4"])
    k = D.add_node(m, "user", "comment", "K", parent_ids=[r])
    D.add_edge(m, "user", "n2", k)
    D.request_merge(m, ["n4", r])
    D.approve(m, "n1")
    D.answer(m, "n2", choice=0)
    D.answer(m, "n3", choice=0)
    D.end_turn(m)
    for n in D.agenda(m):
        if not D.pending_merge(m, n["id"]):
            D.ack(m, n["id"])
    x = D.merge_into(m, "m1", "decision", "X")
    D.close_round(m)
    assert set(D.parents(m, k)) == {x, "n2"}
    assert all("by" in e for e in m["edges"])


def test_merge_requests_must_be_connected_and_convex():
    m = chart()
    assert "goal" in refused(D.request_merge, m, ["n1", "n2"])
    assert "connected" in refused(D.request_merge, m, ["n2", "n3"])
    assert "two" in refused(D.request_merge, m, ["n2"])
    r = D.add_node(m, "user", "comment", "R", parent_ids=["n2"])
    q = D.add_node(m, "user", "comment", "Q", parent_ids=[r])
    D.add_edge(m, "user", "n2", q)
    assert "sits between" in refused(D.request_merge, m, ["n2", q])
    mid = D.request_merge(m, ["n2", r, q])
    assert mid == "m1"
    assert "pending merge" in refused(D.remove_node, m, q)
    assert "pending merge" in refused(D.request_merge, m, [q, "n2"])
    assert D.is_handled(m, D.node(m, "n2"))
    D.cancel_merge(m, mid)
    D.remove_node(m, q)


def test_merge_into_makes_an_ordinary_node_that_can_merge_again():
    m = chart()
    r = D.add_node(m, "user", "comment", "R", parent_ids=["n3"])
    u = D.add_node(m, "user", "comment", "U", parent_ids=[r])
    c = D.add_node(m, "user", "comment", "C", parent_ids=[u])
    mid = D.request_merge(m, ["n3", r, u])
    D.answer(m, "n2", choice=0)
    D.approve(m, "n1")
    D.approve(m, "n4")
    D.end_turn(m)  # n3 is handled by the pending merge
    assert mid in refused(D.close_round, m)
    x = D.merge_into(m, mid, "question", "Budget: used kit is fine?", "condensed", OPTS)
    assert D.parents(m, x) == ["n1"] and D.children(m, x) == [c]
    live = {n["id"] for n in D.live_nodes(m)}
    assert {"n3", r, u}.isdisjoint(live) and x in live and len(m["nodes"]) == 9
    assert "merged away" in refused(D.add_node, m, "agent", "finding", "f", parent_ids=[r])
    ans = next(n["id"] for n in D.agenda(m) if n["type"] == "answer")
    assert {n["id"] for n in D.agenda(m)} == {ans, c}
    D.ack(m, c)
    D.ack(m, ans)
    D.close_round(m)
    assert D.request_merge(m, [x, c]) == "m2"  # the merged node is just a node


def test_store_serializes_writers_and_skips_failed_writes():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".lantern" / "t.json"
        m = D.new_map("t", "G")
        D.close_round(m)
        D.create(path, m)
        assert "already exists" in refused(D.create, path, m)

        def writer():
            for _ in range(25):
                D.mutate(path, lambda m: D.add_node(m, "user", "comment", "r", parent_ids=["n1"]))

        threads = [threading.Thread(target=writer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(D.load(path)["nodes"]) == 51
        before = path.read_bytes()
        refused(D.mutate, path, lambda m: D.edit_node(m, "n1", "x", ""))
        assert path.read_bytes() == before
        assert "No map" in refused(D.load, Path(d) / ".lantern" / "nope.json")


def run_cli(cwd, *args, stdin=""):
    p = subprocess.run([sys.executable, str(HERE / "lantern.py"), *args],
                       cwd=cwd, input=stdin, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def test_map_path_falls_back_to_pre_rename_maps():
    import os
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            assert D.map_path("t") == Path.cwd() / ".lantern" / "t.json"
            (Path(d) / ".dagmap").mkdir()
            (Path(d) / ".dagmap" / "t.json").write_text("{}")
            assert D.map_path("t") == Path.cwd() / ".dagmap" / "t.json"
            (Path(d) / ".lantern").mkdir()
            (Path(d) / ".lantern" / "t.json").write_text("{}")
            assert D.map_path("t") == Path.cwd() / ".lantern" / "t.json"
        finally:
            os.chdir(cwd)


def test_cli_round_trip():
    with tempfile.TemporaryDirectory() as d:
        assert run_cli(d, "new", "t", "--title", "Home lab", "--body-file", "-", stdin="## Destination\nA spec")[0] == 0
        q = ("add", "t", "--type", "question", "--title", "Budget?", "--parent", "n1",
             "--option", "Low::cheap", "--option", "Mid", "--option", "High")
        code, _, err = run_cli(d, *q, stdin="ignored: no --body-file")
        assert code == 1 and "needs a body" in err
        code, out, _ = run_cli(d, *q, "--body-file", "-", stdin="why")
        assert (code, out.strip()) == (0, "n2")
        assert run_cli(d, "close-round", "t")[0] == 0
        assert run_cli(d, "approve", "t", "n1")[0] == 0
        assert run_cli(d, "answer", "t", "n2", "--choice", "1", "--comment", "fine")[1].strip() == "n3"
        assert run_cli(d, "end-turn", "t")[0] == 0
        out = run_cli(d, "agenda", "t")[1]
        assert "ENDED" in out and "n3 answer" in out and 'n2 question "Budget?"' in out
        assert 'picked option 1 ("Mid") · comment: fine' in out
        assert "option 0: Low — cheap" in run_cli(d, "node", "t", "n2")[1]
        code, _, err = run_cli(d, "close-round", "t")
        assert code == 1 and err.startswith("refused:") and "n3" in err
        assert run_cli(d, "add", "t", "--type", "decision", "--title", "Mid budget", "--parent", "n3")[0] == 0
        code, _, err = run_cli(d, "add", "t", "--type", "Decision", "--title", "x", "--parent", "n1")
        assert code == 2 and "invalid choice" in err
        code, _, err = run_cli(d, "add", "t", "--type", "comment", "--title", "x", "--parent", "n1")
        assert code == 1 and "user type" in err
        code, _, err = run_cli(d, "children", "t", "n99")
        assert code == 1 and "No node n99" in err
        assert run_cli(d, "close-round", "t")[0] == 0
        out = run_cli(d, "outline", "t")[1]
        assert out.splitlines()[0].startswith("n1 goal") and "      n4 decision" in out


def test_frontier_closes_only_on_resolution():
    m = chart()
    r = D.add_node(m, "user", "comment", "R", parent_ids=["n4"])
    assert "n4" in {n["id"] for n in D.frontier(m)}  # a remark is discussion, not resolution
    D.answer(m, "n2", choice=0)
    assert "n2" not in {n["id"] for n in D.frontier(m)}
    assert "picked option 0 (\"A\")" in D.render_agenda(m)
    assert "members:" in D.render_node(m, D.request_merge(m, ["n4", r]))


def test_server_applies_user_ops_and_guards_requests():
    with tempfile.TemporaryDirectory() as d:
        run_cli(d, "new", "t", "--title", "G")
        run_cli(d, "add", "t", "--type", "task", "--title", "T", "--parent", "n1")
        run_cli(d, "close-round", "t")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        srv = subprocess.Popen([sys.executable, str(HERE / "lantern.py"), "serve", "t", "--port", str(port)],
                               cwd=d, stdout=subprocess.PIPE, text=True)
        try:
            srv.stdout.readline()  # banner: the server is listening
            base = f"http://127.0.0.1:{port}"

            inst = json.load(urllib.request.urlopen(base + "/api/map"))["inst"]

            def post(body, ctype="application/json", host=None):
                body = {"inst": inst, **body}
                headers = {"Content-Type": ctype, **({"Host": host} if host else {})}
                req = urllib.request.Request(base + "/api/op", data=json.dumps(body).encode(), headers=headers)
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.load(r)
                except urllib.error.HTTPError as e:
                    return e.code, json.load(e)

            view = json.load(urllib.request.urlopen(base + "/api/map"))
            assert (view["round"], view["queue"], view["todo"]) == (2, 2, 2)
            assert "<html" in urllib.request.urlopen(base + "/").read().decode()
            assert post({"op": "approve", "args": {"id": "n2"}}, ctype="text/plain")[0] == 415
            assert post({"op": "approve", "args": {"id": "n2"}}, host="evil.example")[0] == 403
            code, body = post({"op": "approve", "args": {"id": "n9"}})
            assert code == 400 and "No node" in body["error"]
            code, body = post({"op": "approve", "args": {"id": "n1"}, "inst": "stale"})
            assert code == 409 and "Reload" in body["error"]
            assert post({"op": "approve", "args": []})[0] == 400  # args of the wrong shape
            for op in ("close_round", "agenda", "nope"):  # agent ops and reads do not exist over HTTP
                assert "Unknown op" in post({"op": op})[1]["error"]
            assert post({"op": "approve", "args": {"id": "n1"}})[0] == 200
            code, body = post({"op": "approve", "args": {"id": "n2"}})
            assert code == 200 and body["map"]["todo"] == 0
            code, body = post({"op": "end_turn"})
            assert code == 200 and body["map"]["ended"] is True
        finally:
            srv.terminate()
            srv.wait()


if __name__ == "__main__":
    names = sys.argv[1:] or sorted(k for k in globals() if k.startswith("test_"))
    for name in names:
        globals()[name]()
        print("ok", name)
    print(f"{len(names)} passed")
