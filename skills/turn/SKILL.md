---
name: turn
description: Play the agent's turn on a lantern map: read what the user did, diagnose each focus branch, then write merges, rewrites, replies, requests and new frontier, and close the round. Use for `/lantern:turn [slug]`.
disable-model-invocation: true
---

# lantern:turn — the agent's turn

Read `../../RULES.md` (relative to this skill's base directory) first. `L` below is `../../bin/lantern.py`, as an absolute path.

Slug omitted: use the only map in `.lantern/` (or `.dagmap/`, for maps made before the rename). If there are several, ask which one.

A turn has three phases: **read**, **diagnose**, **write**. Write nothing before the write phase.

## 1. Read what the user did

1. `$L agenda <slug>`. If it says `user turn OPEN`, stop and tell the user to press End turn in the UI.
2. Read every item on the agenda with `node`: each pending merge and its members, each unclear node, each request, and each new user node. Read their parents and children too, and `outline` when you need the wider map. A user node can sit under a node from a closed round: the user came back to it.
3. Fix the **focus**, as "Focus" in RULES.md defines it. Everything outside it stays as it is this turn, even unreviewed nodes and open fog.

## 2. Diagnose

For each focus branch, name its bottleneck and choose the technique for it, from "Thinking" in RULES.md. Do this in your head: never write a technique name into a node.

Then decide what the branch needs from you. Many branches need only the replies below. Add new questions only where the bottleneck calls for them, and stay within the question limit for the whole turn.

## 3. Write

1. **Pending merges.** For each, write one node with `merge-into`. That node states where the thread landed, not the conversation that got there. It also answers any comment among the members that asks something, since they get no separate reply. Pick the type from the outcome:
   - the user settled it: `decision`;
   - you established a fact: `finding`;
   - it is still open: `question`, with options.
   Never write `decision` for something the user did not decide. The new node takes over every link from outside nodes into the members and every link from the members out. The members leave the graph.
2. **Unclear nodes.** `rewrite` each one in place: same id and links, so no new node. Make it stand alone, as "Writing for the user" says. A question keeps 3 or 4 options. A node marked `→ REWRITE AS FOG` becomes a fog node: pass no options, and write it as fog: the dim view of an area, not a question or a decision. The rewritten node goes back into the user's review.
3. **Every new user node.** Reply with an agent node that has it as a parent:
   - `answer`: a `decision` recording what it settles, plus any new questions it opens.
   - `comment`: read what it asks for. A question gets a `finding`; if it needs knowledge from outside this directory, research it as the **research** request in RULES.md says, and write the result as the finding. A choice or a rule the user states gets a `decision`. Anything else gets the task, question or fog it calls for, or folds into a sibling's reply.
   One reply may have several parents when it answers several user nodes together. Use `ack` only when no reply would add anything.
4. **Requests.** Answer each request on the agenda as "Requests" in RULES.md says.
5. **Push the frontier in the focus.** Apply what you chose in the diagnose phase. Turn fog that is now sharp enough into questions under it. Add new fog for what is still unclear. Remove nothing: an obsolete branch gets a decision that says so.
6. **Check before you close.** Read `outline` for the focus. Look for nodes that repeat each other, nodes that contradict a decision anywhere on the map, questions over the limit, and nodes that do not stand alone. Fix what you can with new nodes. Then `$L close-round <slug>`. If it is refused, it names what is missing. Handle that and run it again.

## 4. Re-pitch

Tell the user where the map is now, like a re-pitch to someone who just walked in. Give a little context: what got settled this turn and what you found, in the same plain language. Say how far the map is from "Done when" in the goal. Then name, by title, the questions to answer first and why each one unblocks others. Name, in one line, the branches that wait for the user outside this turn's focus. If the user set a focus and nothing in it is open any more, say so and suggest what to focus on next.
