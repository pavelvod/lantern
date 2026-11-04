---
name: map
description: Chart a new lantern map, a round-based brainstorming DAG that leads the user through the fog of a big idea over many sessions. Use for `/lantern:map <idea>`.
disable-model-invocation: true
---

# lantern:map — chart a new map

Read `../../RULES.md` (relative to this skill's base directory) first. `L` below is `../../bin/lantern.py`, as an absolute path.

1. Interview the user to pin down the **destination**: what reaching the end of this map looks like (a spec, a decision, a plan). Settle it before anything else; it fixes the scope.
   - Ask one question at a time, and wait for the answer. Offer 2 to 4 concrete options with your recommendation first; the user can always answer in their own words.
   - Look up facts in the code, docs or files yourself. Ask the user only for what only they know: intent, priorities, limits.
   - Push back on vague answers. Ask for an example, a number, or what "done" looks like.
   - Note the domain terms the user uses, with one-line definitions, and any standing preferences. They go into the goal's Notes.
   - Stop when you can state the destination and "Done when" in a few lines and the user agrees.
2. Pick a kebab-case slug. Create the map; the destination is the goal:
   ```bash
   $L new <slug> --title "<destination in one line>" --body-file - <<'MAP'
   ## Destination
   ...
   ## Notes
   <domain terms with one-line definitions, standing preferences>
   ## Done when
   ...
   MAP
   ```
3. Survey breadth-first across the whole space, not deep on one thread. First list the independent dimensions of the destination (for example users, data, interfaces, failure modes, cost, operations: whichever apply). Every dimension gets at least one node. Add what you find under the goal (or under each other). Make at most 5 of these questions: the ones whose answers shape the rest. Make the remainder tasks and fog.
4. `$L close-round <slug>`. The user's turn starts.
5. Start the UI: read `../serve/SKILL.md` (relative to this skill's base directory) and follow it, then stop. Charting resolves nothing.
