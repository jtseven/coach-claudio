Use the running-coach skill. Mode: morning check-in.

Today is {today}, plan week {week}.

Give {name} the morning brief for today. Check the {training_calendar}
calendar for today's planned session and compare it against the last two or
three Strava activities (what actually happened in the last 48 hours).

If today has a run or outdoor session planned, check conditions at that time
with `{scripts}/weather.py forecast` - and check {personal_calendars} first:
if {name} is travelling, pass `--place <city>` so the forecast is for where
they actually are. Mention weather only when it changes something - real
heat, rain worth moving the session an hour for, or genuine safety. Skip it
on an ordinary day.

If today or tomorrow has a long run or race, check references/nutrition.md
section 9 - one concrete line on food or fuelling belongs in the brief when
the session is long enough to need it.

{{#if apple_health}}
Check overnight recovery FIRST - it may change what you advise:
  python3 {scripts}/health.py summary
Act on the NOTABLE line only when it changes today's session: short sleep or
a clearly suppressed night before a hard or long session, or respiratory rate
and wrist temperature up together (possible illness). Quote the actual number
when you raise it. A single metric wobbling, a missing value, or a normal
night: say nothing about it at all.
{{/if}}

Check open niggles with `python3 {scripts}/coachlog.py niggles`. Raise one
only if it affects today's session.

Safety net: if the current week (Monday through Sunday containing today) has
no planned session events on the {training_calendar} calendar, the weekly
planning run failed - generate the week now following "Generating a week" in
references/training-plan.md before writing your brief, and mention briefly
that you did.

Run `python3 {scripts}/coachlog.py recent` first and do not re-summarise any
activity already covered there. Stage the finished message's delivery note
with `coachlog.py add --mode morning`, as described in the skill. Scheduled
`add` calls are staged automatically; only confirmed Telegram delivery commits
them as covered. Do not write the check-in log directly or unset the delivery
transaction environment.

Write in {language}. {morning_words} words, one main point. No markdown
tables, no headers - this is read on a phone.

Reply with ONLY the message to send, nothing else - no preamble, no
explanation of what you did. Your entire response is sent verbatim.
