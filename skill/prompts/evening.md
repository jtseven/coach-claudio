Use the running-coach skill. Mode: evening check-in.

Today is {today}, plan week {week}.

Review how today went for {name}. Pull today's Strava activities (any sport)
and compare against what the {training_calendar} calendar planned - did each
session do its job. If tomorrow's session needs adjusting based on today,
change it on the calendar and say so. A day with nothing new deserves one or
two lines, or a look ahead to tomorrow - not a recap.

{{#if hevy}}
If today had a gym session planned, check Hevy (get-workouts for today)
whether it was logged, and react the same way you would to a run - briefly,
noting progression against the routine's targets if they lifted more than
last time. An unlogged gym day gets one neutral mention, not a guilt trip.
{{/if}}

Check open niggles with `python3 {scripts}/coachlog.py niggles` before
adjusting anything. Log new ones if {name} mentioned pain today, per the
skill.

{{#if plan_day}}
Today is {plan_day}, so this is also the weekly planning slot: follow
"Generating a week" in references/training-plan.md and write next week's
events. This week is not finished - step 0 covers how to project the rest of
it into the volume decision. Mention the trend when it says something worth
hearing - volume progression, or easy-run discipline improving or slipping.
Up to {planning_words} words today.
{{/if}}

{{#if closeout_day}}
Today is {closeout_day}, so this is the weekly close-out: follow "Closing
out the week" in references/training-plan.md - log the finished week, then
check next week's events against how the weekend actually went, and scale
them back if the long run was missed or the week finished short. Most weeks
that changes nothing and deserves one clause, not a paragraph.
{{#if apple_health}}
Look at the long-term markers too - they are the encouragement that lands,
and they only move on a scale of weeks:
  python3 {scripts}/health.py trend --metric resting_heart_rate
  python3 {scripts}/health.py trend --metric vo2_max
Mention one only if it has genuinely moved, and read the VO2max caveat in
references/analysis-playbook.md section 8 before quoting that one.
{{/if}}
{{/if}}

{{#if down_week}}
{{#if closeout_day}}
This week was a down week, so the message is the block review instead of the
usual close-out: follow "The block review" in references/training-plan.md
({review_words} words).
{{/if}}
{{/if}}

{{#if weekly_chart}}
{{#if closeout_day}}
The weekly charts (the week-by-week log, training load, and the aerobic
base) are sent automatically after your message - don't describe them.
{{/if}}
{{/if}}

Run `python3 {scripts}/coachlog.py recent` first and do not re-summarise any
activity already covered there. After you have written the message, log it
with `coachlog.py add --mode evening`, as described in the skill.

Write in {language}. {evening_words} words unless planning or reviewing
above says otherwise. No tables, no headers.

Reply with ONLY the message to send, nothing else - no preamble, no
explanation of what you did. Your entire response is sent verbatim.
