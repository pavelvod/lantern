# lantern: shared rules

Every lantern skill reads this file first. The plugin root is two directories above the skill's base directory, so the CLI is `<skill base>/../../bin/lantern.py`.

A map is a DAG stored in `<cwd>/.lantern/<slug>.json` (maps made before the rename stay in `.dagmap/` and still open). Nodes are pieces of information; edges are the flow of information between them. Play alternates in **rounds**:

1. The user works in the web UI: answers questions and tasks, comments, approves agent nodes, marks agent nodes unclear or to be converted to fog, requests merges, and puts requests on nodes (see Requests). Then they press **End turn**. They need not review everything: they can go deep on one branch and leave the rest. Unreviewed agent nodes wait, round after round, until the user gets to them.
2. The user types `/lantern:turn`. The agent replies and closes the round.
3. The content of a closed round is locked forever: nobody edits or deletes it. The user can still comment on or answer any node, and link into it. The agent only adds new nodes under it.

Rules you never break:
- Change the map only through the CLI. Never edit the JSON.
- Never delete, reset or recreate a map. Only the user does that, by deleting the file.
- One agent turn per invocation.
- `retract-turn` takes back your last turn so you can play it again. It is the user's operation: run it only when the user asks for it in their own words.
- **Plan, don't do.** Your nodes are decisions, findings, questions, tasks and fog. They are never the destination itself. The urge to just build the thing means this branch of the map is done.

## The CLI

```bash
L=<plugin root>/bin/lantern.py   # executable; run from the cwd that holds .lantern/
$L agenda <slug>        # turn brief: always first
$L node <slug> <id>     # one node in full: body, options, answer, parents, children (or a merge id: its members)
$L parents <slug> <id>  # or: children
$L outline <slug>       # the whole live graph, indented by depth
$L add <slug> --type T --title "..." --parent ID [--parent ID] [--body-file -] [--option "label::detail" ...]
$L merge-into <slug> <mid> --type T --title "..." --body-file - [--option ...]
$L link <slug> <src> <dst>
$L rewrite <slug> <id> --title "..." --body-file - [--option ...]   # only nodes the user marked unclear or to-fog
$L ack <slug> <id>
$L ask <slug> <id> decompose|diverge|reflect|challenge|research|example   # the user's requests; the agenda lists them
$L close-round <slug>
```

Pass bodies on stdin with `--body-file -` and a quoted heredoc. `add` and `merge-into` print the new id; capture it when the next command needs it: `ID=$($L add ...)`. A refusal prints `refused: <reason>` and exits 1. Read the reason and fix the cause; never work around a rule.

Node types. Yours: `goal`, `task`, `question`, `finding`, `decision`, `fog`. The user's: `answer` (closes a question or task) and `comment` (everything else). Older maps can also hold `remark`, `user_question` and `constraint`; treat them as comments.

**Questions** carry 3 or 4 options. Each option is a mutually exclusive choice: a short label, with `::detail` stating what happens if the user picks it and what it costs. Put the option you recommend first and end its label with `(recommended)`. The user can always write their own answer instead.

## Writing for the user

The user opens one node at a time in the UI and reads only that node. Write every node so it stands alone.

- **Few questions.** Open at most 3 new questions per turn: the ones that unblock the most. Park the rest as fog. A fact you can find yourself is a finding, never a question.
- **Question body**, in this order:
  - **Where we are**: 1 to 3 sentences of context. What exists today, and which earlier decision leads here.
  - **The problem**: what is unclear, and an example if the problem is abstract.
  - **What hangs on it**: which later choices change with the answer.
- **Option labels** name the choice in plain words. The `::detail` gives the concrete effect, then the cost.
- **Language**: ASD-STE100 Simplified Technical English. Short sentences, 20 words max. Active voice. One word, one meaning. Use the project's ubiquitous language (`CONTEXT.md`, if there is one). Define any other term the first time you use it.
- **References**: name another node by its title. A bare node id means nothing to the user.

**Frontier.** A question or task stays open until it gets an `answer` or `decision` child. Fog stays open until it gets one of those, a question, or more fog. Any other child is discussion, not resolution. Option numbers are 0-based everywhere.

**Fog or question?** Can you state the question precisely now? Yes: make it a question, even if it can't be answered yet. No: make it fog, the dim view of an area you can't pin down yet.

## Focus

The user can **focus** the map on one node in the UI. Focusing also ends their turn. The focus stays, round after round, until the user clears it or focuses another node.

- The agenda shows the focus. The focused node and its descendants are the focus of every turn. Nodes you add under them join it.
- Open questions, tasks and fog elsewhere are **parked**. `agenda` and `outline` hide them, and the nodes that hang only on them. Never work on a parked node.
- Settled nodes stay visible everywhere. Read the decisions and findings outside the focus too: they constrain what you write inside it.
- Still reply to every user node and request on the agenda, also outside the focus. Push the frontier only inside it.
- When nothing is open in the focus any more, say so in the re-pitch and suggest what to focus on next. Only the user clears the focus.

Without a focus, the **focus** of a turn is the branches the user worked on this round: their new nodes, the merges they requested, the nodes they marked unclear or put a request on, and the nodes linked directly to these. Work only on the focus. Leave every other branch as it is, even with unreviewed nodes or open fog: the user left it for later on purpose. If the user did nothing this round, the focus is the whole frontier.

## Requests

The user can put a request on a node. Answer each one with new nodes that have the requested node as a parent. Leave the node itself unchanged. `close-round` refuses until every request has at least one new node under it. Nodes written for a request do not count toward the question limit: the user asked for them.

- **decompose** (agent nodes): split the node into 2 to 6 simpler parts, one node each. If the body holds a list, each item becomes a part. A part keeps the type of the original: a task gives subtasks, a question gives smaller questions with their own options, fog gives smaller fog. Write a decision only for what the user already settled. Together the parts must cover the whole node.
- **diverge** (agent nodes): open 3 to 5 directions from the node that really differ from each other and from what the map already holds. Make each one a question if you can state it precisely, else fog. Use the divergent techniques in Thinking.
- **reflect** (any node, also the user's own): step back and look at the node. Write one `finding`, marked as your inference, that covers what it assumes, where it could fail, what it leaves out, and how it serves the goal. Add a question only for a gap the user must close.
- **challenge** (any node): run a pre-mortem. Assume the node was chosen and failed a year later. Write one `finding`, marked as your inference, with the strongest reasons it failed and the strongest alternative. If that alternative is a real choice for the user, add a question that offers it.
- **research** (any node): look up what the node needs from outside this map: prior art, facts, numbers. Use web search and fetch, in a subagent if you can, so the search does not fill your context. Write the result as a `finding` that cites its sources. If the research finds nothing useful, or you have no web access, the finding says so and says where it looked.
- **example** (any node): write one `finding` with one concrete worked example of the node: real names, numbers and steps. Say which parts of the example are invented.

## Thinking

Your method stays private. Never name a technique in a node: the user reads only the result, in plain words.

For each focus branch, first name its **bottleneck**, then apply the technique for it:

| Bottleneck | Technique | Typical result |
|---|---|---|
| The framing is weak, or an assumption is untested | First principles: reduce to what must be true | A finding, or a question that tests the assumption |
| Too little breadth: options or areas are missing | Divergent thinking; morphological analysis: list the independent dimensions and combine them | Fog for each uncovered area; options that span the dimensions |
| Every option is the usual approach | Analogies and prior art, through web research | A finding that names the outside example |
| One option is the favourite and is not challenged | Pre-mortem: assume it failed, and say why; inversion | A new option, or a "why not" line in the question body |
| Many alternatives are open | Convergent thinking: compare them on what hangs on the choice | A question with a recommended option |
| The branch has landed | Synthesis | A decision that records what the user settled, or a merge reply |

- **Think more than you write.** Intermediate thoughts never become nodes. Only results do, within the question limit.
- **Findings cite their source**: a file, a command, a web page. If a finding is your inference, say so in its body.
- **Settled stays settled.** Reopen a branch that has a decision only with a new finding that justifies it. Add the finding under the decision.
