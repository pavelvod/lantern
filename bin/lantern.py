#!/usr/bin/env python3
"""lantern: a round-based brainstorming map stored as a DAG in <cwd>/.lantern/<slug>.json.

One stdlib-only file: every rule, the CLI the agent uses, and the HTTP server the browser UI
uses. Rounds alternate: the user works in the UI and ends their turn, then the agent replies
and closes the round. A closed round's content is locked; the user can still comment on it
and link into it. See RULES.md.
"""
import argparse
import copy
import fcntl
import json
import os
import secrets
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AGENT_TYPES = ("goal", "task", "question", "finding", "decision", "fog")
USER_TYPES = ("answer", "comment")
LEGACY_USER_TYPES = ("remark", "user_question", "constraint")  # older maps; read-only now
FRONTIER_TYPES = ("question", "task", "fog")
# Requests the user puts on a node; the agent answers each with new nodes under it. See RULES.md.
ASKS = ("decompose", "diverge", "reflect", "challenge", "research", "example")
AGENT_ONLY_ASKS = ("decompose", "diverge")
# A frontier node is open until something resolves it; any other child is just discussion.
RESOLVED_BY = {"question": ("answer", "decision"), "task": ("answer", "decision"),
               "fog": ("answer", "decision", "question", "fog")}
ENDED = "The user's turn is ended. Unlock it in the UI to change anything."
NOT_ENDED = "The user's turn is still open. Wait until they press End turn."


class Refused(Exception):
    """An operation broke a map rule. The message is shown to the user or the agent as-is."""


# ---------- queries ----------

def node(m, nid):
    for n in m["nodes"]:
        if n["id"] == nid:
            return n
    raise Refused(f"No node {nid}.")


def is_locked(m, item):
    return item["round"] < m["round"]


def folded(m):
    """Merged-away original id -> id of the node it was merged into."""
    return {i: g["into"] for g in m["merges"] if g["into"] for i in g["members"]}


def pending_merge(m, nid):
    return next((g for g in m["merges"] if not g["into"] and nid in g["members"]), None)


def live_nodes(m):
    f = folded(m)
    return [n for n in m["nodes"] if n["id"] not in f]


def live_edges(m):
    f = folded(m)
    return [e for e in m["edges"] if e["from"] not in f and e["to"] not in f]


def parents(m, nid):
    return [e["from"] for e in live_edges(m) if e["to"] == nid]


def children(m, nid):
    return [e["to"] for e in live_edges(m) if e["from"] == nid]


def reaches(m, start, target):
    stack, seen = [start], set()
    while stack:
        c = stack.pop()
        if c == target:
            return True
        if c not in seen:
            seen.add(c)
            stack.extend(e["to"] for e in m["edges"] if e["from"] == c)
    return False


def replies(m, nid, author, this_round=False):
    kids = [node(m, c) for c in children(m, nid)]
    return [c for c in kids if c["author"] == author and (not this_round or c["round"] == m["round"])]


def answer_of(m, qid):
    return next((c for c in replies(m, qid, "user") if c["type"] == "answer"), None)


def _reviewed_before(m, n):
    """The user approved or replied to this version of n in an earlier round than this one."""
    r0, r1 = n["round"], m["round"]
    return (any(r0 < m.get(k, {}).get(n["id"], 0) < r1 for k in ("approved",) + ASKS)
            or any(r0 < c["round"] < r1 for c in replies(m, n["id"], "user")))


def review_queue(m):
    """Agent nodes the user has not reviewed yet. They stay here, round after round, until the user
    approves, answers, comments on or marks them; ending the turn does not require it."""
    return [n for n in live_nodes(m)
            if n["author"] == "agent" and n["round"] < m["round"] and not _reviewed_before(m, n)]


# Round marks: m[kind] maps node id -> the round it was marked in; a mark counts only in that round.
# kind: "approved" (user), "unclear" (user: the agent rewrites the node), "to_fog" (user: an unclear
# mark that also turns the node into fog; always set with "unclear"), "acked" (agent: no reply needed),
# and one kind per request in ASKS (user).

def has(m, kind, nid):
    return m.get(kind, {}).get(nid) == m["round"]


def marked(m, kind):
    return [i for i, r in m.get(kind, {}).items() if r == m["round"]]


def mark(m, kind, nid):
    m.setdefault(kind, {})[nid] = m["round"]


def unmark(m, kind, nid):
    m.get(kind, {}).pop(nid, None)


def asks(m, nid):
    return [k for k in ASKS if has(m, k, nid)]


def is_handled(m, n):
    return (has(m, "approved", n["id"]) or has(m, "unclear", n["id"]) or bool(asks(m, n["id"]))
            or bool(replies(m, n["id"], "user", this_round=True))
            or pending_merge(m, n["id"]) is not None)


def agenda(m):
    """User nodes of this round that the agent must reply to or ack."""
    return [n for n in live_nodes(m)
            if n["author"] == "user" and n["round"] == m["round"] and pending_merge(m, n["id"]) is None]


def focus(m):
    """The node the user focused on, or None. Set with focus_on; it lasts until the user changes it."""
    f = m.get("focus")
    return f and folded(m).get(f["id"], f["id"])  # a merged focus moves to the node it became


def parked(m):
    """Ids hidden while a focus is set: open frontier nodes outside the focused subtree and off the
    path to it, plus whatever is reachable only through them. Settled nodes stay visible everywhere."""
    fid = focus(m)
    if not fid:
        return set()
    # ponytail: one reaches() per node, O(n·e); fine for maps of a few hundred nodes
    scope = {fid} | {n["id"] for n in live_nodes(m) if reaches(m, fid, n["id"])}
    path = {n["id"] for n in live_nodes(m) if reaches(m, n["id"], fid)}
    roots = {n["id"] for n in frontier(m)} - scope - path
    goal = next(n["id"] for n in live_nodes(m) if n["type"] == "goal")
    seen, stack = set(), [goal]
    while stack:
        c = stack.pop()
        if c not in seen and c not in roots:
            seen.add(c)
            stack.extend(children(m, c))
    return {n["id"] for n in live_nodes(m)} - seen


def frontier(m):
    return [n for n in live_nodes(m) if n["type"] in FRONTIER_TYPES
            and not any(node(m, c)["type"] in RESOLVED_BY[n["type"]] for c in children(m, n["id"]))]


# ---------- operations: check everything first, then mutate ----------

def _guard(m, who):
    if who == "user" and m["ended"]:
        raise Refused(ENDED)
    if who == "agent" and not m["ended"]:
        raise Refused(NOT_ENDED)


def _options(raw):
    if not 3 <= len(raw) <= 4:
        raise Refused("A question needs 3 or 4 options.")
    out = []
    for o in raw:
        label = (o.get("label") or "").strip()
        if not label:
            raise Refused("Every option needs a label.")
        out.append({"label": label, **({"detail": o["detail"].strip()} if o.get("detail") else {})})
    return out


def new_map(slug, title, body=""):
    """Round 1 belongs to the agent (ended=True) so it can chart; close_round hands over."""
    m = {"slug": slug, "round": 1, "seq": 1, "mseq": 1, "ended": True,
         "nodes": [], "edges": [], "approved": {}, "acked": {}, "merges": []}
    add_node(m, "agent", "goal", title, body)
    _snapshot(m)
    return m


def add_node(m, who, ntype, title, body="", parent_ids=(), options=None, choice=None):
    _guard(m, who)
    if ntype not in (AGENT_TYPES if who == "agent" else USER_TYPES):
        raise Refused(f"The {who} cannot create a {ntype!r} node.")
    title = (title or "").strip()
    if not title:
        raise Refused("A title is required.")
    parent_ids = list(dict.fromkeys(parent_ids))
    if ntype == "goal":
        if parent_ids or any(n["type"] == "goal" for n in m["nodes"]):
            raise Refused("A map has exactly one goal, with no parents.")
    elif not parent_ids:
        raise Refused("Every node except the goal needs a parent.")
    f = folded(m)
    for p in parent_ids:
        node(m, p)
        if p in f:
            raise Refused(f"{p} was merged away. Use {f[p]} instead.")
    if ntype == "answer":
        _answerable(m, parent_ids)
    n = {"id": f"n{m['seq']}", "type": ntype, "author": who, "title": title, "body": body or "", "round": m["round"]}
    if ntype == "question":
        if who == "agent" and not (body or "").strip():
            raise Refused("A question needs a body: where we are, the problem, what hangs on it. Pass --body-file -.")
        n["options"] = _options(options or [])
    if choice is not None:
        n["choice"] = choice
    m["seq"] += 1
    m["nodes"].append(n)
    m["edges"].extend({"from": p, "to": n["id"], "round": m["round"], "by": who} for p in parent_ids)
    return n["id"]


def _answerable(m, parent_ids):
    """An answer hangs under exactly one open question or task that is not being rewritten or merged."""
    if len(parent_ids) != 1 or node(m, parent_ids[0])["type"] not in ("question", "task"):
        raise Refused("An answer goes under exactly one question or task.")
    q = parent_ids[0]
    if answer_of(m, q):
        raise Refused(f"{q} is already answered. Delete that answer to change it.")
    if has(m, "unclear", q):
        raise Refused(f"{q} is marked unclear. Withdraw the mark first, or wait for the rewrite.")
    if pending_merge(m, q):
        raise Refused(f"{q} is in a pending merge. Cancel the merge first.")


def add_edge(m, who, src, dst):
    _guard(m, who)
    b = node(m, dst)
    a = node(m, src)
    f = folded(m)
    if src in f or dst in f:
        raise Refused("Merged-away nodes cannot be linked.")
    if src == dst:
        raise Refused("A node cannot link to itself.")
    if who == "agent" and is_locked(m, b):
        raise Refused(f"{dst} is locked (round {b['round']}). Only the user links into closed rounds.")
    if b["type"] == "answer" and a["type"] in ("question", "task"):
        raise Refused("An answer belongs to its one question or task. Answer this one on its own.")
    if any(e["from"] == src and e["to"] == dst for e in m["edges"]):
        raise Refused("Already linked.")
    if reaches(m, dst, src):
        raise Refused("That link would make a loop.")
    m["edges"].append({"from": src, "to": dst, "round": m["round"], "by": who})
    if not _merges_fold(m):
        m["edges"].pop()
        raise Refused("That link would put a node between members of a pending merge. Cancel the merge first.")


def remove_edge(m, src, dst):
    _guard(m, "user")
    live = live_edges(m)
    e = next((e for e in live if e["from"] == src and e["to"] == dst), None)
    if not e:
        raise Refused("No such link.")
    if is_locked(m, e):
        raise Refused(f"That link is locked (round {e['round']}).")
    if sum(1 for x in live if x["to"] == dst) == 1:
        raise Refused(f"That is {dst}'s last parent link. Delete {dst} instead.")
    i = m["edges"].index(e)
    del m["edges"][i]
    if not _merges_fold(m):
        m["edges"].insert(i, e)
        raise Refused("That would cut a pending merge in two. Cancel the merge first.")


def _own_editable(m, nid):
    n = node(m, nid)
    if n["author"] != "user" or is_locked(m, n):
        raise Refused(f"{nid} is locked or not yours.")
    return n


def remove_node(m, nid):
    _guard(m, "user")
    _own_editable(m, nid)
    if pending_merge(m, nid):
        raise Refused(f"{nid} is in a pending merge. Cancel the merge first.")
    if any(e["from"] == nid for e in m["edges"]):
        raise Refused(f"{nid} has replies. Delete its replies first.")
    for k in ASKS:
        unmark(m, k, nid)
    m["nodes"] = [n for n in m["nodes"] if n["id"] != nid]
    m["edges"] = [e for e in m["edges"] if e["to"] != nid]


def edit_node(m, nid, title, body):
    _guard(m, "user")
    n = _own_editable(m, nid)
    title = (title or "").strip()
    if not title:
        raise Refused("A title is required.")
    if "choice" in n and title != n["title"]:
        del n["choice"]  # a picked option, retitled, is the user's own answer now
    n["title"], n["body"] = title, body or ""


def close_round(m):
    """Finish the agent's turn. Everything from this round locks."""
    _guard(m, "agent")
    pend = [g["id"] for g in m["merges"] if not g["into"]]
    if pend:
        raise Refused(f"Resolve pending merges first: {', '.join(pend)}.")
    unclear = marked(m, "unclear")
    if unclear:
        raise Refused(f"Rewrite these unclear nodes first: {', '.join(unclear)}.")
    undone = [f"{k} {i}" for k in ASKS for i in marked(m, k) if not replies(m, i, "agent", this_round=True)]
    if undone:
        raise Refused(f"Answer these requests with new nodes under them first: {', '.join(undone)}.")
    missing = [n["id"] for n in agenda(m)
               if not replies(m, n["id"], "agent", this_round=True) and not has(m, "acked", n["id"])]
    if missing:
        raise Refused(f"Reply to or ack these user nodes first: {', '.join(missing)}.")
    m["round"] += 1
    m["ended"] = False


def mark_unclear(m, nid, fog=False):
    """The user flags an agent node as unclear; the agent rewrites it in place on its turn.
    With fog=True the rewrite also turns it into a fog node."""
    _guard(m, "user")
    n = node(m, nid)
    if n["author"] != "agent":
        raise Refused(f"{nid} is yours. Edit it instead.")
    if n not in review_queue(m):
        raise Refused(f"{nid} is already reviewed. Comment on it instead.")
    if fog and n["type"] in ("fog", "goal"):
        raise Refused(f"{nid} is a {n['type']}; it cannot become fog.")
    if nid in folded(m) or pending_merge(m, nid):
        raise Refused(f"{nid} is merged or in a pending merge.")
    if answer_of(m, nid):
        raise Refused(f"{nid} is already answered. Delete the answer first.")
    if asks(m, nid):
        raise Refused(f"{nid} has a request ({', '.join(asks(m, nid))}). Withdraw it first.")
    unmark(m, "approved", nid)
    mark(m, "unclear", nid)
    (mark if fog else unmark)(m, "to_fog", nid)


def unmark_unclear(m, nid):
    _guard(m, "user")
    if not has(m, "unclear", nid):
        raise Refused(f"{nid} is not marked unclear this round.")
    unmark(m, "unclear", nid)
    unmark(m, "to_fog", nid)


def request(m, nid, kind):
    """The user asks the agent for one of ASKS on a node, on its next turn."""
    _guard(m, "user")
    n = node(m, nid)
    if kind not in ASKS:
        raise Refused(f"Unknown request {kind!r}. Use one of: {', '.join(ASKS)}.")
    if kind in AGENT_ONLY_ASKS and n["author"] != "agent":
        raise Refused(f"Only agent nodes can be asked to {kind}.")
    if nid in folded(m) or pending_merge(m, nid):
        raise Refused(f"{nid} is merged or in a pending merge.")
    if has(m, "unclear", nid):
        raise Refused(f"{nid} is marked unclear. Withdraw the mark first.")
    mark(m, kind, nid)


def focus_on(m, nid):
    """The user narrows the map to nid and its descendants, and ends the turn. The goal unfocuses."""
    _guard(m, "user")
    n = node(m, nid)
    if nid in folded(m) or pending_merge(m, nid):
        raise Refused(f"{nid} is merged or in a pending merge.")
    m["focus"] = None if n["type"] == "goal" else {"id": nid, "round": m["round"]}
    end_turn(m)


def unfocus(m):
    _guard(m, "user")
    if not focus(m):
        raise Refused("There is no focus to clear.")
    m["focus"] = None


def unrequest(m, nid, kind):
    _guard(m, "user")
    if kind not in asks(m, nid):
        raise Refused(f"{nid} has no {kind} request this round.")
    unmark(m, kind, nid)


def rewrite(m, nid, title, body, options=None):
    """The agent rewrites an unclear node in place: same id and links, new text, back in review."""
    _guard(m, "agent")
    if not has(m, "unclear", nid):
        raise Refused(f"{nid} is not marked unclear this round.")
    n = node(m, nid)
    to_fog = has(m, "to_fog", nid)
    kind = "fog" if to_fog else n["type"]
    title = (title or "").strip()
    if not title:
        raise Refused("A title is required.")
    if kind == "question" and not (body or "").strip():
        raise Refused("A question needs a body. Pass --body-file -.")
    opts = _options(options or []) if kind == "question" else None
    n.setdefault("history", []).append({"at": m["round"], "round": n["round"], "title": n["title"],
                                        "body": n["body"], "type": n["type"], "to_fog": to_fog,
                                        **({"options": n["options"]} if "options" in n else {})})
    n["title"], n["body"], n["round"], n["type"] = title, body or "", m["round"], kind
    if opts:
        n["options"] = opts
    elif to_fog:
        n.pop("options", None)
    unmark(m, "unclear", nid)
    unmark(m, "to_fog", nid)


def _state(m):
    return {k: v for k, v in m.items() if k != "before"}


def _snapshots(m):
    b = m.get("before") or []
    return [b] if isinstance(b, dict) else b  # a single dict: maps from before snapshots had rounds


def _snapshot(m):
    """Keep the map as it is when the agent's turn begins, so retract_turn can restore it.
    Keeps this round's and the previous round's: retract in round r needs round r-1's."""
    keep = [b for b in _snapshots(m) if b["round"] == m["round"] - 1]
    m["before"] = keep + [copy.deepcopy(_state(m))]


def retract_turn(m):
    """The user takes back the agent's last turn: the map goes back to how it was when the
    agent's turn began, and the agent plays that round again. Only before the user has done
    anything this round."""
    _guard(m, "user")
    r = m["round"]
    if r < 2:
        raise Refused("There is no agent turn to take back.")
    if (any(x["round"] == r for x in m["nodes"] + m["edges"] + m["merges"])
            or any(marked(m, k) for k in ("approved", "unclear") + ASKS)
            or (m.get("focus") or {}).get("round") == r):
        raise Refused("You already worked this round. Delete your work first.")
    before = next((b for b in _snapshots(m) if b["round"] == r - 1), None)
    if not before:
        raise Refused("That turn was played before lantern kept turn snapshots. It cannot be taken back.")
    seq, mseq = m["seq"], m["mseq"]  # never hand out a retracted id again
    m.clear()
    m.update(copy.deepcopy(before), seq=seq, mseq=mseq)
    m["before"] = [before]  # the replayed turn can be taken back too


def answer(m, qid, choice=None, text=None, comment=""):
    """The user's answer: a picked option (choice = index) or their own text. The comment is the body."""
    q = node(m, qid)
    if q["type"] != "question":
        raise Refused(f"{qid} is not a question.")
    if choice is not None:
        opts = q.get("options", [])
        if not 0 <= choice < len(opts):
            raise Refused("No such option.")
        title = opts[choice]["label"]
    else:
        title = text
    return add_node(m, "user", "answer", title, comment, [qid], choice=choice)


def approve(m, nid):
    _guard(m, "user")
    n = node(m, nid)
    if n not in review_queue(m):
        raise Refused(f"{nid} is already reviewed, so it needs no approval.")
    if n["type"] == "question":
        raise Refused("Answer a question instead of approving it.")
    unmark(m, "unclear", nid)
    unmark(m, "to_fog", nid)
    mark(m, "approved", nid)


def unapprove(m, nid):
    _guard(m, "user")
    if not has(m, "approved", nid):
        raise Refused(f"{nid} is not approved this round.")
    unmark(m, "approved", nid)


def end_turn(m):
    """Unreviewed agent nodes may stay: the user can go deep on one branch and leave the rest."""
    _guard(m, "user")
    m["ended"] = True
    _snapshot(m)


def unlock(m):
    if not m["ended"]:
        raise Refused("The turn is not ended.")
    if not any(b["round"] == m["round"] and b == _state(m) for b in _snapshots(m)):
        raise Refused("The agent has started its turn. Wait until it closes the round; "
                      "then retract the turn if it should be played again.")
    m["ended"] = False


def ack(m, nid):
    """The agent saw a user node and has nothing useful to add."""
    _guard(m, "agent")
    if node(m, nid) not in agenda(m):
        raise Refused(f"{nid} is not on this round's agenda.")
    mark(m, "acked", nid)

# ---------- merge: the agent condenses a connected group into one ordinary node ----------

def _connected(group, edges):
    start = next(iter(group))
    seen, stack = {start}, [start]
    while stack:
        c = stack.pop()
        for e in edges:
            o = e["to"] if e["from"] == c else e["from"] if e["to"] == c else None
            if o in group and o not in seen:
                seen.add(o)
                stack.append(o)
    return seen == group


def _merges_fold(m, extra=None):
    """True if every pending merge (plus `extra` ids) can still fold: each group stays connected,
    and the graph with each group folded into one node has no loop."""
    groups = [set(g["members"]) for g in m["merges"] if not g["into"]] + ([set(extra)] if extra else [])
    edges = live_edges(m)
    if not all(_connected(g, edges) for g in groups):
        return False
    rep = {i: f"#{k}" for k, g in enumerate(groups) for i in g}
    adj = {}
    for e in edges:
        a, b = rep.get(e["from"], e["from"]), rep.get(e["to"], e["to"])
        if a != b:
            adj.setdefault(a, set()).add(b)
    state = {}  # 1: on the current path, 2: done
    for root in adj:
        stack = [(root, iter(adj.get(root, ())))]
        if state.get(root):
            continue
        state[root] = 1
        while stack:
            v, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[v] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                return False
            elif not state.get(nxt):
                state[nxt] = 1
                stack.append((nxt, iter(adj.get(nxt, ()))))
    return True


def request_merge(m, ids):
    _guard(m, "user")
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2:
        raise Refused("Mark at least two nodes to merge.")
    f = folded(m)
    for i in ids:
        n = node(m, i)
        if n["type"] == "goal":
            raise Refused("The goal cannot be merged.")
        if i in f:
            raise Refused(f"{i} was already merged away.")
        if pending_merge(m, i):
            raise Refused(f"{i} is already in a pending merge.")
        if has(m, "unclear", i) or asks(m, i):
            raise Refused(f"{i} is marked unclear or has a request. Withdraw it first.")
    group, edges = set(ids), live_edges(m)
    if not _connected(group, edges):
        raise Refused("Marked nodes must be connected to each other.")
    # Folding must not create a loop: nothing outside may sit on a path between two members.
    for e in edges:
        if e["from"] in group and e["to"] not in group and any(reaches(m, e["to"], i) for i in ids):
            raise Refused(f"{e['to']} sits between marked nodes. Mark it too, or unmark one side.")
    if not _merges_fold(m, ids):
        raise Refused("Folding this together with the other pending merges would make a loop. "
                      "Cancel one of them, or mark them as one merge.")
    mid = f"m{m['mseq']}"
    m["mseq"] += 1
    m["merges"].append({"id": mid, "members": ids, "round": m["round"], "into": None})
    return mid


def cancel_merge(m, mid):
    _guard(m, "user")
    g = next((g for g in m["merges"] if g["id"] == mid and not g["into"]), None)
    if not g:
        raise Refused(f"No pending merge {mid}.")
    m["merges"].remove(g)


def merge_into(m, mid, ntype, title, body="", options=None):
    """Write the one node a pending merge becomes. It takes over the group's outside links."""
    _guard(m, "agent")
    g = next((g for g in m["merges"] if g["id"] == mid and not g["into"]), None)
    if not g:
        raise Refused(f"No pending merge {mid}.")
    group, edges = set(g["members"]), live_edges(m)
    ins = list(dict.fromkeys(e["from"] for e in edges if e["to"] in group and e["from"] not in group))
    outs = list(dict.fromkeys(e["to"] for e in edges if e["from"] in group and e["to"] not in group))
    nid = add_node(m, "agent", ntype, title, body, ins, options)
    m["edges"].extend({"from": nid, "to": o, "round": m["round"], "by": "agent"} for o in outs)
    g["into"] = nid
    return nid

# ---------- storage: one writer at a time, atomic replace ----------

def map_path(slug):
    p = Path.cwd() / ".lantern" / f"{slug}.json"
    old = Path.cwd() / ".dagmap" / f"{slug}.json"  # maps made before the rename
    return old if not p.exists() and old.exists() else p


def load(path):
    if not path.exists():
        raise Refused(f"No map at {path}.")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, m):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def create(path, m):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{path}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            raise Refused(f"{path} already exists. Delete it yourself to start over.")
        _write(path, m)


def mutate(path, fn):
    """Apply fn(map) under an exclusive lock and save. Nothing is written if fn raises."""
    load(path)  # fail before creating a lock file next to a missing map
    with open(f"{path}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        m = load(path)
        out = fn(m)
        _write(path, m)
        return out

# ---------- text for agents: compact, one node per line ----------

def line(m, n):
    tags = []
    if has(m, "approved", n["id"]):
        tags.append("approved")
    g = pending_merge(m, n["id"])
    if g:
        tags.append(f"in merge {g['id']}")
    tail = f"  [{', '.join(tags)}]" if tags else ""
    return f"{n['id']} {n['type']} ({n['author']}, r{n['round']}) {n['title']}{tail}"


def short(m, nid):
    n = node(m, nid)
    return f'{nid} {n["type"]} "{n["title"]}"'


def _flat(text, limit=300):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def render_agenda(m):
    goal = next(n for n in m["nodes"] if n["type"] == "goal")
    turn = "user turn ENDED: your turn" if m["ended"] else "user turn OPEN: not your turn yet"
    out = [f"map {m['slug']} · round {m['round']} · {turn}", f"goal: {line(m, goal)}"]
    hidden = parked(m)
    if focus(m):
        out += [f"FOCUS: {line(m, node(m, focus(m)))}",
                f"  work only on it and its descendants; {len(hidden)} parked nodes are hidden. See Focus in RULES.md."]
    pend = [g for g in m["merges"] if not g["into"]]
    if pend:
        out += ["", "PENDING MERGES (resolve each with merge-into; read members with node):"]
        out += [f"  {g['id']}: " + "; ".join(short(m, i) for i in g["members"]) for g in pend]
    unclear = marked(m, "unclear")
    if unclear:
        out += ["", "UNCLEAR (rewrite each in place with rewrite; read it with node first):"]
        out += ["  " + line(m, node(m, i)) + ("  → REWRITE AS FOG: no options; it becomes a fog node"
                                              if has(m, "to_fog", i) else "") for i in unclear]
    reqs = [(k, i) for k in ASKS for i in marked(m, k)]
    if reqs:
        out += ["", "REQUESTS (answer each with new nodes under it; see Requests in RULES.md):"]
        out += [f"  {k}: " + line(m, node(m, i)) for k, i in reqs]
    ag = agenda(m)
    out += ["", f"NEW FROM USER THIS ROUND ({len(ag)}; reply to each, or ack):"]
    for n in ag:
        out.append("  " + line(m, n))
        out.append("      ← " + "; ".join(short(m, p) for p in parents(m, n["id"])))
        if n["type"] == "answer":
            how = f'picked option {n["choice"]} ("{n["title"]}")' if "choice" in n else "own answer, not an option"
            out.append(f"      {how}" + (f" · comment: {_flat(n['body'])}" if n["body"] else ""))
        elif n["body"]:
            out.append("      " + _flat(n["body"]))
    if not ag:
        out.append("  (none)")
    approved = marked(m, "approved")
    if approved:
        out += ["", "APPROVED THIS ROUND:"] + ["  " + short(m, i) for i in approved]
    out += ["", "FRONTIER (open questions, tasks, fog):"]
    out += ["  " + line(m, n) for n in frontier(m) if n["id"] not in hidden] or ["  (none)"]
    return "\n".join(out)


def render_node(m, nid):
    g = next((g for g in m["merges"] if g["id"] == nid), None)
    if g:
        state = f"resolved into {g['into']}" if g["into"] else "pending: resolve with merge-into"
        return f"merge {nid} (round {g['round']}, {state})\nmembers:\n" + "\n".join("  " + line(m, node(m, i)) for i in g["members"])
    n = node(m, nid)
    out = [line(m, n)]
    f = folded(m)
    if nid in f:
        out.append(f"merged away into {f[nid]}")
    out.append("parents: " + ("; ".join(short(m, p) for p in parents(m, nid)) or "-"))
    out.append("children: " + ("; ".join(short(m, c) for c in children(m, nid)) or "-"))
    for i, o in enumerate(n.get("options", [])):
        out.append(f"  option {i}: {o['label']}" + (f" — {o['detail']}" if o.get("detail") else ""))
    if n["type"] == "question":
        a = answer_of(m, nid)
        out.append("answer: " + (short(m, a["id"]) if a else "-"))
    out += ["", n["body"] or "(no body)"]
    return "\n".join(out)


def render_outline(m):
    goal = next(n for n in live_nodes(m) if n["type"] == "goal")
    out, seen, hidden = [], set(), parked(m)

    def walk(nid, depth):
        pad = "  " * depth
        if nid in seen:
            out.append(f"{pad}↑ {nid} (shown above)")
            return
        seen.add(nid)
        out.append(pad + line(m, node(m, nid)))
        for c in children(m, nid):
            if c not in hidden:
                walk(c, depth + 1)

    walk(goal["id"], 0)
    if hidden:
        out.append(f"({len(hidden)} parked nodes hidden: outside the focus)")
    return "\n".join(out)


# ---------- ops: every map operation once, for both front doors ----------

TYPES = f"agent: {', '.join(AGENT_TYPES)}; user: {', '.join(USER_TYPES)}"
TYPE = ("--type", {"required": True, "choices": AGENT_TYPES + USER_TYPES, "metavar": "TYPE", "help": TYPES})
TITLE = ("--title", {"required": True})
BODY = ("--body-file", {"dest": "body", "metavar": "BODY_FILE", "help": "path, or - for stdin"})  # the CLI reads it into the body
PARENT = ("--parent", {"action": "append", "required": True, "dest": "parents", "metavar": "PARENT"})
OPTION = ("--option", {"action": "append", "dest": "options", "metavar": "OPTION",
                       "help": "repeatable; 'label' or 'label::detail'. A question needs 3 or 4, recommended first"})
EDGE = (("from", {"metavar": "src"}), ("to", {"metavar": "dst"}))
ASK = ("kind", {"choices": ASKS})

# name -> (side, CLI help, CLI args, fn(m, actor, args)). side: who may reach the op: "read", "user",
# "agent", or "any" (the author follows from the caller). The rule functions still guard the turn.
# CLI args are argparse specs whose dest names are the HTTP arg names.
OPS = {
    "agenda": ("read", "turn brief: start every agent turn here", (), lambda m, w, a: render_agenda(m)),
    "outline": ("read", "the whole live graph, indented by depth", (), lambda m, w, a: render_outline(m)),
    "node": ("read", "one node in full, or a merge id (m1) with its members", ("id",),
             lambda m, w, a: render_node(m, a["id"])),
    "children": ("read", "nodes this one leads to", ("id",),
                 lambda m, w, a: "\n".join(line(m, node(m, c)) for c in children(m, node(m, a["id"])["id"])) or "(none)"),
    "parents": ("read", "nodes this one comes from", ("id",),
                lambda m, w, a: "\n".join(line(m, node(m, p)) for p in parents(m, node(m, a["id"])["id"])) or "(none)"),
    "add": ("any", "add a node; the author follows from the type; prints the new id", (TYPE, TITLE, PARENT, BODY, OPTION),
            lambda m, w, a: add_node(m, w or _cli_author(m, a["type"]), a["type"],
                                     a["title"], a.get("body", ""), a["parents"], a.get("options"))),
    "link": ("any", "add an edge (agent)", EDGE, lambda m, w, a: add_edge(m, w or "agent", a["from"], a["to"])),
    "unlink": ("user", "remove an edge (user)", EDGE, lambda m, w, a: remove_edge(m, a["from"], a["to"])),
    "edit": ("user", "edit your node from this round (user)", ("id", TITLE, BODY),
             lambda m, w, a: edit_node(m, a["id"], a["title"], a.get("body", ""))),
    "rm": ("user", "delete your node from this round (user)", ("id",), lambda m, w, a: remove_node(m, a["id"])),
    "answer": ("user", "answer a question (user)",
               ("id", ("--choice", {"type": int}), ("--text", {}), ("--comment", {"default": ""})),
               lambda m, w, a: answer(m, a["id"], a.get("choice"), a.get("text"), a.get("comment", ""))),
    "approve": ("user", "approve an agent node (user)", ("id",), lambda m, w, a: approve(m, a["id"])),
    "unapprove": ("user", "withdraw an approval (user)", ("id",), lambda m, w, a: unapprove(m, a["id"])),
    "merge": ("user", "request a merge of connected nodes (user)", (("ids", {"nargs": "+"}),),
              lambda m, w, a: request_merge(m, a["ids"])),
    "cancel-merge": ("user", "cancel a pending merge (user)", ("id",), lambda m, w, a: cancel_merge(m, a["id"])),
    "end-turn": ("user", "end the user's turn (user)", (), lambda m, w, a: end_turn(m)),
    "unlock": ("user", "reopen the user's turn (user)", (), lambda m, w, a: unlock(m)),
    "retract-turn": ("user", "take back the agent's last turn so it is played again (user; only before any work "
                     "this round)", (), lambda m, w, a: retract_turn(m)),
    "merge-into": ("agent", "resolve a pending merge with one node (agent). The node takes over the members' "
                   "outside parents and children; the members leave the graph. Prints the new id",
                   ("id", TYPE, TITLE, BODY, OPTION),
                   lambda m, w, a: merge_into(m, a["id"], a["type"], a["title"], a.get("body", ""), a.get("options"))),
    "unclear": ("user", "mark an agent node as unclear: the agent rewrites it next turn (user)", ("id",),
                lambda m, w, a: mark_unclear(m, a["id"])),
    "to-fog": ("user", "mark an agent node to be rewritten as fog next turn (user)", ("id",),
               lambda m, w, a: mark_unclear(m, a["id"], fog=True)),
    "clear-unclear": ("user", "withdraw an unclear or to-fog mark (user)", ("id",),
                      lambda m, w, a: unmark_unclear(m, a["id"])),
    "ask": ("user", "ask the agent to work on a node next turn: decompose, diverge, reflect, challenge, research, example (user)", ("id", ASK),
            lambda m, w, a: request(m, a["id"], a["kind"])),
    "focus": ("user", "focus the map on a node and its descendants, and end the turn (user)", ("id",),
              lambda m, w, a: focus_on(m, a["id"])),
    "unfocus": ("user", "clear the focus (user)", (), lambda m, w, a: unfocus(m)),
    "unask": ("user", "withdraw a request (user)", ("id", ASK), lambda m, w, a: unrequest(m, a["id"], a["kind"])),
    "rewrite": ("agent", "rewrite a node the user marked unclear, in place: same id and links (agent)",
                ("id", TITLE, BODY, OPTION),
                lambda m, w, a: rewrite(m, a["id"], a["title"], a.get("body", ""), a.get("options"))),
    "ack": ("agent", "mark an agenda node as needing no reply (agent)", ("id",), lambda m, w, a: ack(m, a["id"])),
    "close-round": ("agent", "finish the agent's turn (agent)", (), lambda m, w, a: close_round(m)),
}


def _cli_author(m, ntype):
    if ntype in AGENT_TYPES:
        return "agent"
    if m["ended"]:
        raise Refused(f"{ntype!r} is a user type. The agent writes {', '.join(AGENT_TYPES)}.")
    return "user"


def run(m, actor, name, args):
    """Apply op `name` (kebab or snake case) to m and return its result. actor None is the CLI: it
    reaches every op. actor "user" is HTTP: it reaches only user ops, and acts as the user. The caller
    loads or saves m. Raises Refused; a missing arg raises KeyError."""
    side, _, _, fn = OPS.get(name.replace("_", "-"), (None,) * 4)
    if not fn or actor and side not in (actor, "any"):
        raise Refused(f"Unknown op {name!r}.")
    return fn(m, actor, args)


# ---------- HTTP (the browser's front door; user operations only) ----------


def view(m, mtime):
    """The live graph plus derived flags, for the UI. Merged-away nodes are left out."""
    queue, hidden = {n["id"] for n in review_queue(m)}, parked(m)
    open_ids = {n["id"] for n in frontier(m)}
    nodes = []
    for n in live_nodes(m):
        v = dict(n, locked=is_locked(m, n), in_queue=n["id"] in queue,
                 handled=n["id"] in queue and is_handled(m, n), approved=has(m, "approved", n["id"]),
                 unclear=has(m, "unclear", n["id"]), asks=asks(m, n["id"]), parked=n["id"] in hidden, to_fog=has(m, "to_fog", n["id"]), open=n["id"] in open_ids,
                 rewritten=bool(n.get("history")) and n["history"][-1]["at"] == m["round"] - 1)
        g = pending_merge(m, n["id"])
        if g:
            v["merge"] = g["id"]
        if n["type"] == "question":
            a = answer_of(m, n["id"])
            v["answer"] = a["id"] if a else None
        nodes.append(v)
    return {"slug": m["slug"], "round": m["round"], "ended": m["ended"], "mtime": mtime, "focus": focus(m), "nodes": nodes,
            "edges": [dict(e, locked=is_locked(m, e)) for e in live_edges(m)],
            "queue": len(queue - hidden), "todo": sum(1 for v in nodes if v["in_queue"] and not v["handled"] and not v["parked"]),
            "agenda": len(agenda(m))}




def serve(path, port):
    load(path)  # fail fast on a missing map
    ui = (Path(__file__).resolve().parent / "ui.html").read_bytes()
    inst = secrets.token_hex(8)  # a tab from another server run must not act on this map
    hosts = set()

    class Handler(BaseHTTPRequestHandler):
        timeout = 10  # a silent socket must not hold a thread forever

        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _view(self):
            mtime = path.stat().st_mtime_ns  # before the read: a write in between shows up on the next poll
            return dict(view(load(path), mtime), inst=inst)

        def do_GET(self):
            if self.headers.get("Host") not in hosts:
                return self._send(403, {"error": "Bad Host header."})
            if self.path == "/":
                return self._send(200, ui, "text/html; charset=utf-8")
            if self.path != "/api/map":
                return self._send(404, {"error": "Not found."})
            try:
                return self._send(200, self._view())
            except (Refused, OSError, ValueError) as e:
                return self._send(500, {"error": str(e)})

        def do_POST(self):
            if self.headers.get("Host") not in hosts:
                return self._send(403, {"error": "Bad Host header."})
            if self.path != "/api/op":
                return self._send(404, {"error": "Not found."})
            # A JSON content type forces a CORS preflight, which this server never answers: blocks CSRF.
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._send(415, {"error": "Send JSON."})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if req.get("inst") != inst:
                    return self._send(409, {"error": "This page belongs to another server run. Reload it."})
                out = mutate(path, lambda m: run(m, "user", str(req.get("op")), req.get("args") or {}))
            except Refused as e:
                return self._send(400, {"error": str(e)})
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                return self._send(400, {"error": f"Bad request: {e!r}"})
            except OSError as e:
                return self._send(500, {"error": str(e)})
            self._send(200, {"result": out, "map": self._view()})

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # writes still serialize on the map's flock
    port = srv.server_address[1]  # the real one, for --port 0
    hosts.update({f"127.0.0.1:{port}", f"localhost:{port}"})  # blocks DNS-rebinding
    print(f"lantern: http://127.0.0.1:{port}/  ({path})", flush=True)
    srv.serve_forever()


# ---------- CLI (the agent's front door) ----------

def _body(path):
    if not path:
        return ""
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _opts(raw):
    return [dict(zip(("label", "detail"), o.split("::", 1))) for o in raw or []]


def _parser():
    p = argparse.ArgumentParser(prog="lantern.py", description="Round-based brainstorming map. "
                                "Run from the directory that holds .lantern/.")
    sub = p.add_subparsers(dest="op", required=True, metavar="op")

    def op(name, help, args):
        sp = sub.add_parser(name, help=help)
        sp.add_argument("slug")
        for x in args:
            n, kw = (x, {}) if isinstance(x, str) else x
            sp.add_argument(n, **kw)

    op("new", "create a map and its goal (agent; round 1)", (TITLE, BODY))
    for name, (_, help, args, _) in OPS.items():
        op(name, help, args)
    op("serve", "serve the web UI on 127.0.0.1", (("--port", {"type": int, "default": 8765}),))
    return p


def _run(a, path):
    if a.op == "new":
        create(path, new_map(a.slug, a.title, _body(a.body)))
        return f"created {path}: goal n1, round 1. Add the first nodes, then close-round."
    if a.op == "serve":
        return serve(path, a.port)
    args = vars(a)
    if "body" in args:
        args["body"] = _body(args["body"])  # read stdin before taking the lock
    if "options" in args:
        args["options"] = _opts(args["options"])
    if OPS[a.op][0] == "read":
        return run(load(path), None, a.op, args)
    return mutate(path, lambda m: run(m, None, a.op, args)) or "ok"


def cli(argv=None):
    a = _parser().parse_args(argv)
    try:
        out = _run(a, map_path(a.slug))
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(cli())
