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

If it was a run, record it in the run log once - it feeds the efficiency
and zone trends the weekly close-out reads. Pull its streams (time,
heart_rate, velocity_smooth, moving, distance, altitude, plus watts if it has
power) at resolution 300, save the JSON to a temp file, and run:
  python3 {scripts}/analyze.py streams --streams <file> --append --id {activity_id} --date <start_local>
It prints decoupling and time in zones too; mention them only if they are
the interesting thing about this run.

{{#if apple_health}}
If it was a run, also check the watch's biomechanics:
  python3 {scripts}/health.py run
Mention cadence, ground contact or vertical oscillation only when something
genuinely stands out against their usual numbers - section 9 of the playbook
explains why these are noisy daily aggregates and what is actually
actionable. Do not recite form numbers that look normal.
{{/if}}

Write in {language}. {postrun_words} words, no tables, no headers. Stage
its delivery note with `coachlog.py add --mode postrun --ids {activity_id}`,
as described in the skill. Scheduled `add` calls are staged automatically;
only confirmed Telegram delivery commits them as covered. Do not write the
check-in log directly or unset the delivery transaction environment. SKIP
stages nothing and does not claim that a message was delivered.

Reply with ONLY the message to send (or SKIP), nothing else. Your entire
response is sent verbatim.
