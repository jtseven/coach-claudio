Use the running-coach skill. Mode: post-run reaction.

{name} just finished and uploaded Strava activity {activity_id} (sport type:
{sport_type}). This message arrives within the hour after the workout - it is
a reaction, not a briefing.

First run: python3 {scripts}/coachlog.py covered {activity_id}
If it prints covered, reply with exactly the single word SKIP and nothing
else - it means a conversation or check-in already discussed it.

Otherwise: pull the activity and react the way the skill describes for "just
did my run / ride / session" - lead with whether it did the job the calendar
intended, then the one genuinely interesting thing in the data. For a run
read references/analysis-playbook.md first; for anything else see
references/cross-training.md.

{{#if apple_health}}
If it was a run, also check the watch's biomechanics:
  python3 {scripts}/health.py run
Mention cadence, ground contact or vertical oscillation only when something
genuinely stands out against their usual numbers - section 9 of the playbook
explains why these are noisy daily aggregates and what is actually
actionable. Do not recite form numbers that look normal.
{{/if}}

Write in {language}. {postrun_words} words, no tables, no headers. Then log
it with `coachlog.py add --mode postrun --ids {activity_id}`, as described in
the skill.

Reply with ONLY the message to send (or SKIP), nothing else. Your entire
response is sent verbatim.
