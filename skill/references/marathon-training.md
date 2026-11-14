# Marathon prescription and race execution

Read alongside `training-plan.md` when designing a marathon block, introducing
quality, reviewing long runs, or choosing race pace. Individual targets belong
in `athlete/plan.md`. The examples below are coaching starting points, not
validated injury thresholds or a requirement to reach every dose.

## Decide from completed training

Use recent complete weeks, repeatable run frequency, symptoms, perceived recovery
and all other sport. A good HR trace alone does not establish tolerance. Record
what was completed separately from what is projected. Old targets are not a
distance debt to catch up.

When introducing or extending sustained quality, hold weekly running distance
and long-run length broadly steady. When lengthening the long run, keep its
intensity familiar. Reconsider added gym sets, hills, downhill running and races
as increases too. This is a practical attribution/recovery rule, not proof that
simultaneous increases always cause injury. Do not make up missed kilometres
by extending the run immediately before a long run.

If a runner repeatedly misses a fourth run, fix its slot before lengthening the
other three. An optional fifth short run can distribute an established workload;
introduce it by redistributing existing distance first, not automatically adding
volume. More days are useful only when they fit the athlete's life and recovery.

## Session patterns

Start at the lower end, progress only after the existing dose is absorbed, and
count warm-up, recovery and cool-down in weekly km. Usually allow an easy/rest
day between demanding runs. Replace easy running with quality rather than adding
the whole quality session on top of it.

| Purpose | Starting example | Progression and recovery |
| --- | --- | --- |
| Easy/base | Conversational running; optionally 4–6 relaxed strides of 15–20 s after an easy run | Full walk/jog recovery between strides; stop before fatigue. Base need not universally forbid quality, but building consistency takes priority for low-volume or returning runners. |
| Steady introduction | 2 × 6–8 min comfortably purposeful, below threshold | 2–3 min easy jog; progress toward 3 × 8 min only while the rest of the week stays familiar. |
| Threshold | 3 × 5 min at controlled, repeatable effort, roughly RPE 7/10 | 2 min jog; later 3 × 8 or 2 × 12 min. Around 15–30 min work is an example range, not an entitlement. Avoid a race effort on the last rep. |
| Current marathon effort | 2 × 10–15 min within an otherwise easy medium/long run | 3–5 min easy; progress total work before making blocks continuous. Later 6–12 km total MP work may fit; 12–16 km needs an established specific-build baseline and recovery evidence. |
| Easy long run | A familiar distance/duration at conversational effort | Increase one dimension at a time, using the session review below. A long run with MP blocks is that week's quality session too. |

A hilly group run can be demanding despite a low average HR. A hard parkrun or
tune-up race counts as quality; strides with full recovery are a small speed
exposure, not a threshold workout. Start with at most one sustained quality
session weekly when developing frequency/volume; an MP long run replaces it.

**Marathon pace has three distinct meanings:** a model's equivalent, a desired
finish-time pace, and a reviewed current sustainable marathon pace. Prescribe
MP work using the third. Name its evidence and date in the plan; if uncertain,
use controlled marathon effort and a provisional range. Do not choose the
fastest number printed by `paces`, VDOT or Riegel. Threshold pace also follows
current evidence, not a stale short-race result.

## Long-run review

Before increasing, compare the proposal with the longest run in the preceding
30 days, in distance **and duration**, and examine terrain/descent, effort,
fuelling, next-day response and adjacent sessions. Use:

```bash
python3 <skill>/scripts/analyze.py session --date 2026-10-11 \
  --proposed-km 18 --proposed-minutes 115 --weekly-km 40
```

This reads the run log; missing runs mean the reference may be incomplete.
A >10% distance increase is an advisory evidence flag, not a causal boundary,
an automatic veto, or clearance below it. Compare duration separately; the
distance study did not validate a matching duration threshold.

Review long-run share as a concentration of work rather than enforcing a
universal percentage. A run near half the week's km deserves particular
attention to supporting runs and recovery, especially with MP work. Sessions
approaching/exceeding about three hours need an explicit benefit/recovery
discussion; shorten them when their cost compromises the following week.
Neither three hours nor a particular share is a proven safety limit.

Do not require three runs over 30 km or a 34 km maximum for everyone. Several
well-absorbed shorter long runs may be preferable. Put the last demanding
long run about 3–4 weeks before the race as a starting convention, with no
late attempt to catch up missed long runs.

At each block review, project the remaining build from actual tolerated volume,
including down weeks, travel, tune-ups and taper. `progression --peak-km ...
--weeks-to-peak ...` gives only an optimistic arithmetic upper bound. If even
that misses the ambition, revise the peak, time goal or race choice; do not
raise the policy just to make the original plan work.

## Tune-ups, goals and pacing

Treat Riegel and VDOT as related equivalents, not independent confirmations or
a prediction interval. A flat, well-paced half roughly 5–8 weeks out can inform
the goal, but conversion also needs the preceding 6–8 weeks of running volume,
long-run execution, MP effort and fuelling tolerance. Hilly/windy tune-ups test
execution and effort; do not force their time into a flat-road prediction.
There is no universal minimum weekly km that guarantees a finish time.

The tune-up replaces quality/long-run work rather than being added. Give its
following days easy/rest options according to the race effort and recovery.
Record an ambition and a plausible current range separately; no time is a
guaranteed floor. Do not invent a mileage-adjusted prediction without a
specified, applicable model and inputs.

Write a race plan before race week: start wave/time, travel, rehearsed breakfast,
carbohydrate/fluid plan, kit, pace range and fallback. On a flat course in
suitable conditions, start the first 3–5 km no faster than the reviewed target,
then aim for even effort/approximately even splits; a modest negative split
is an option if controlled late. Do not bank time. Hills, congestion and heat
change pace at a given effort. In unexpected heat, revise the target and effort
before starting; do not insist on a fixed seconds/km correction.

## Taper and after the race

Use a 2–3-week marathon taper as a starting point. Base reductions on a recent
representative, completed normal week, not a theoretical peak or a down week.
For example: about 75–85% of baseline three weeks out, 55–65% two weeks out,
and 35–45% in pre-race running during race week. **Exclude the marathon itself**
from that final percentage. These examples adapt to baseline, fatigue and race
weekday; they are not three mandatory reductions on top of each other.

Retain familiar run frequency where practical and small amounts of familiar
intensity, while reducing hard minutes, long-run duration and gym volume.
No new workouts, catch-up mileage or soreness-producing strength sessions.
Schedule the last heavy leg session far enough out to recover, commonly
7–10 days; individual soreness history can require more.

After the marathon, start with rest and comfortable everyday movement. Resume
short easy running only when daily activities are comfortable and recovery
permits; build frequency/duration before intensity. Do not resume peak training
on a calendar deadline or prescribe a mechanical reverse taper. Persistent or
worsening pain or systemic symptoms need professional assessment.

## Evidence and limits

- [Buist 2008 randomized trial](https://pubmed.ncbi.nlm.nih.gov/17940147/): the graded programme did not reduce injuries; a weekly 10% rule is not established protection.
- [Frandsen 2025 cohort](https://bjsm.bmj.com/content/59/17/1203): session-distance spikes associated with overuse injury; observational, not proof of a safe/unsafe boundary.
- [Fokkema 2020 cohort](https://pubmed.ncbi.nlm.nih.gov/32421886/): volume/long-run associations with performance do not prove a required dose.
- [Vickers & Vertosick 2016](https://doi.org/10.1186/s13102-016-0052-y): recreational marathon prediction benefits from training context; short-race extrapolation is often optimistic.
- [Wang 2023 taper meta-analysis](https://pmc.ncbi.nlm.nih.gov/articles/PMC10171681/): supports reduced volume with maintained intensity/frequency across endurance populations; exact marathon schedules above are coaching examples.
- [Støren 2008 randomized trial](https://pubmed.ncbi.nlm.nih.gov/18460997/): heavy strength can improve economy; it does not establish guaranteed injury prevention or unilateral superiority.
