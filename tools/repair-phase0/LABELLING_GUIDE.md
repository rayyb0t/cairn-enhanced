# Labelling guide: was the run progressing or stuck at the review point?

Each folder in the evaluation set is one intent whose explore run hit its time limit. For each one,
read the transcript up to the review point and write one label in `labels.csv`. Phase 2 runs the
repair agent in shadow mode on the same runs and compares its decisions with your labels, so the
labels are the answer key.

## The review point

The repair agent decides when 75–80% of the run's budget has passed. `intent.json` gives that window
for each attempt (`review_window`): in seconds from the start of the run, and in the dispatcher's
clock. Session files usually record UTC times, so count from the first entry of the run.

Label only from what the transcript shows **up to the end of that window**. The agent will not see
anything later, so you shouldn't either. What happened afterwards goes in `notes` (see below).

## Labels

`progressing`: more time would probably produce a better fact. Signs, up to the review point:

- The last stretch (roughly the last fifth of the time so far) produced new information: a new finding, a tool result that changed what the agent knows, or a step of its plan completed.
- A long tool is running and visibly advancing: an extraction, scan, build, brute force or decompilation reporting progress, with a clear next step that depends on its result.
- The agent has a concrete plan for the remaining steps, and its recent actions follow that plan.

`stuck`: more time would probably not help. Signs:

- The same command or approach repeated three or more times with the same failure.
- Waiting on something that will not finish: a hung process, an interactive prompt, repeated network timeouts, a tool that has printed nothing for a long time with no reason to expect output.
- Going in circles: re-reading the same files or re-running the same checks without new information.
- Drifting away from the intent's question, or saying it is blocked without a way forward.

`unclear`: the transcript is missing or cut short, or the signs point both ways. Say which in `notes`.

When unsure between `progressing` and `stuck`, ask: "if this run had been given one more slice of
time at this point, would its fact most likely be better?" Yes means `progressing`.

## Columns

- `intent_id`: filled in already.
- `label`: `progressing`, `stuck` or `unclear`.
- `evidence`: where you saw it and a short description, for example `t+22:40 extraction reports 70% done; new partition found at t+21:10`. Use times or line numbers in the transcript. Describe in your own words and don't paste secrets or target data.
- `notes`: anything else. Most useful: what happened after the review point. Did the run finish its step before the limit? Did the conclude fallback's fact miss something the transcript had found? Would one more slice have been enough, or would it have needed to keep going in the background?

## Two made-up examples

These are invented to show the style. They do not come from any real run.

1. Intent: "Identify the file system inside the lab firmware image and list its configuration files." The budget is 30 minutes, so the window is 22:30–24:00.
   - By 21:00 the agent has found the image header and the compression type.
   - At 22:10 it starts extracting the 80 MB image, and the tool prints its offset every few seconds.
   - At 23:50 the extraction is at about 70%, and the agent's last message says it will list `/etc` once it finishes.
   - Label: `progressing`. Evidence: `t+22:10 extraction started; t+23:50 at 70%, steady progress; next step stated`. Notes: `limit hit at 30:00 before extraction finished; fallback fact only had the header details`.
2. Intent: "Check whether the lab web service's admin page accepts the documented default credentials." The budget is 30 minutes, so the window is 22:30–24:00.
   - From 08:00 onwards the agent sends the same login request nine times. Every attempt ends in a connection timeout.
   - Between attempts it re-reads the same notes file.
   - It never tries another port or path, and it has found nothing new since 08:00.
   - Label: `stuck`. Evidence: `t+08:00–t+23:40 same request repeated 9x, all timeouts; no new information`. Notes: `nothing changed after the window either`.
