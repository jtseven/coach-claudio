"""Offline synthetic CLI regressions; standard-library unittest."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skill/scripts/analyze.py'


class ProgressionQuality(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / 'athlete/state').mkdir(parents=True)
        self.log = self.home / 'weeks.jsonl'

    def cli(self, *args):
        return subprocess.run(['python3', str(SCRIPT), '--json', *args],
                              env={**os.environ, 'COACH_HOME': str(self.home),
                                   'HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1',
                                   'XDG_CACHE_HOME': str(self.home/'cache')},
                              capture_output=True, text=True)

    def history(self, last=22):
        rows = [dict(week=i+1, week_of=f'2026-09-{d:02}', actual_km=k,
                     week_kind='normal' if i<3 else 'planned-down',
                     tolerated=True, coverage_complete=True)
                for i, (d, k) in enumerate(zip([7,14,21,28], [30,32,31,last]))]
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        return rows

    def test_planned_down_return_uses_recent_tolerated_baseline(self):
        self.history()
        p = self.cli('progression', '--log', str(self.log), '--as-of', '2026-10-05',
                     '--mode', 'planned-down', '--reason', 'scheduled recovery',
                     '--evidence', 'completed normal weeks without symptoms', '--proposed-km', '31')
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertTrue(out['accepted'])
        self.assertEqual(out['baseline_km'], 31)
        self.assertEqual(out['max_next_week_km'], 31)


    def review(self, *args, mode='build', proposed='20'):
        p = self.cli('progression', '--log', str(self.log), '--as-of', '2026-10-05',
                     '--mode', mode, '--proposed-km', proposed, '--reason', 'review',
                     '--evidence', 'synthetic completed history', *args)
        return p, json.loads(p.stdout)

    def test_zero_week_cannot_be_called_a_planned_down_return(self):
        self.history(last=0)
        p, out = self.review(mode='planned-down')
        self.assertEqual(p.returncode, 2)
        self.assertFalse(out['accepted'])

    def test_sparse_incomplete_history_cannot_validate_even_a_hold(self):
        self.log.write_text(json.dumps(dict(week_of='2026-09-28', actual_km=22))+'\n')
        p, out = self.review(proposed='22')
        self.assertEqual(p.returncode, 2)
        self.assertIsNone(out['max_next_week_km'])


    def test_rollups_share_policy_instead_of_legacy_down_week_ceiling(self):
        self.history()
        for command in ('trend', 'block'):
            p = self.cli(command, '--log', str(self.log), '--as-of', '2026-10-05',
                         '--mode', 'planned-down', '--reason', 'scheduled recovery',
                         '--evidence', 'reviewed tolerated weeks')
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual(out['progression']['max_next_week_km'], 31)
            if command == 'trend':
                self.assertEqual(out['summary']['max_next_week_km'], 31)

    def test_weeklog_records_explicit_quality_and_tolerance(self):
        p = self.cli('weeklog', '--activities', '[]', '--week-of', '2026-09-28',
                     '--week-kind', 'travel', '--coverage-complete', '--tolerated',
                     '--note', 'travel; swimming only')
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out['week_kind'], 'travel')
        self.assertTrue(out['coverage_complete'])
        self.assertTrue(out['tolerated'])


    def test_load_unknown_effort_and_stale_history_suppress_bands(self):
        acts = [dict(id=1, sport_type='Run', start_local='2026-08-01', relative_effort=25),
                dict(id=2, sport_type='Run', start_local='2026-09-28')]
        p = self.cli('load', '--activities', json.dumps(acts), '--until', '2026-10-20')
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertIsNone(out['form_band'])
        self.assertEqual(out['quality']['missing_effort'], 1)
        self.assertEqual(out['quality']['known_effort'], 1)
        self.assertTrue(out['quality']['stale'])
        self.assertEqual(out['quality']['confidence'], 'low')
        self.assertIn('observed', out['load_basis'])

    def test_runlog_coverage_counts_not_just_missing_ids(self):
        acts = [dict(id=i, sport_type='Run', start_local=f'2026-09-{d}')
                for i, d in [(1, '07'), (2, '14')]]
        (self.home/'athlete/state/run_log.jsonl').write_text(
            json.dumps(dict(id='1', date='2026-09-07'))+'\n')
        p = self.cli('runlog', '--missing', json.dumps(acts))
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out['quality']['expected_runs'], 2)
        self.assertEqual(out['quality']['logged_runs'], 1)
        self.assertEqual(out['quality']['missing_runs'], 1)
        self.assertEqual(out['quality']['date_start'], '2026-09-07')
        self.assertEqual(out['quality']['date_end'], '2026-09-14')

    def test_efficiency_and_zones_show_sparse_stale_missingness(self):
        rows = [dict(id='1', date='2026-09-07', sport='Run', moving_s=1800,
                     pct_above_ceiling=5, ef=1.2, intensity_s=dict(low=900,mid=0,high=0)),
                dict(id='2', date='2026-09-14', sport='Run', moving_s=1800)]
        (self.home/'athlete/state/run_log.jsonl').write_text(
            ''.join(json.dumps(r)+'\n' for r in rows))
        for command in ('efficiency', 'zonedist'):
            p = self.cli(command, '--as-of', '2026-10-20')
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual(out['quality']['logged_runs'], 2)
            self.assertEqual(out['quality']['usable_runs'], 1)
            self.assertTrue(out['quality']['stale'])
            self.assertEqual(out['quality']['confidence'], 'low')
            self.assertIsNone(out['quality']['coverage_complete'])
            self.assertIn('limitations', out)
        self.assertEqual(out['quality']['unclassified_moving_s'], 2700)


    def test_weeklog_effort_is_observed_and_missingness_is_explicit(self):
        acts = [dict(id=1, sport_type='Run', start_local='2026-09-28', distance=5000),
                dict(id=2, sport_type='Run', start_local='2026-09-29', distance=5000,
                     relative_effort=0)]
        p = self.cli('weeklog', '--activities', json.dumps(acts), '--week-of', '2026-09-28')
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out['run_effort'], 0)
        self.assertEqual(out['effort_quality']['run_missing'], 1)
        self.assertEqual(out['effort_quality']['run_known'], 1)
        self.assertIn('observed', out['effort_quality']['basis'])

    def test_empty_usable_trends_still_report_quality(self):
        row = dict(id='1', date='2026-09-28', sport='Run', moving_s=1800)
        (self.home/'athlete/state/run_log.jsonl').write_text(json.dumps(row)+'\n')
        for command in ('efficiency', 'zonedist'):
            p = self.cli(command, '--as-of', '2026-10-05')
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual(out['quality']['usable_runs'], 0)
            self.assertEqual(out['quality']['confidence'], 'low')

    def test_illness_cannot_be_overridden_by_build_label(self):
        rows = self.history(last=8)
        rows[-1]['week_kind'] = 'illness'
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        for mode in ('build', 'planned-down', 'travel-return'):
            p, out = self.review(mode=mode, proposed='9')
            self.assertEqual(p.returncode, 2)
            self.assertIsNone(out['max_next_week_km'])
        p, out = self.review(mode='illness-pain-return', proposed='8')
        self.assertEqual(p.returncode, 2)
        p, out = self.review('--return-cap-km', '10', mode='illness-pain-return', proposed='8')
        self.assertEqual(p.returncode, 0)
        self.assertEqual(out['max_next_week_km'], 10)
        p, out = self.review('--return-cap-km', '40', mode='illness-pain-return', proposed='20')
        self.assertEqual(p.returncode, 2)
        self.assertEqual(out['max_next_week_km'], 15.5)

    def test_travel_zero_week_with_cross_training_is_not_running_credit(self):
        rows = self.history(last=0)
        rows[-1].update(week_kind='travel', other_aerobic_h=12)
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        p, out = self.review(mode='travel-return', proposed='20')
        self.assertEqual(p.returncode, 0)
        self.assertEqual(out['max_next_week_km'], 23.2)
        p, out = self.review(mode='travel-return', proposed='31')
        self.assertEqual(p.returncode, 2)
        self.assertIn('cross-training', ' '.join(out['reasons']))

    def test_stale_partial_duplicate_and_nonfinite_proposals_rejected(self):
        self.history()
        for value in ('-1', 'nan', 'inf', '80'):
            p, out = self.review(mode='planned-down', proposed=value)
            self.assertEqual(p.returncode, 2)
        p = self.cli('progression', '--log', str(self.log), '--as-of', '2026-12-01',
                     '--proposed-km', '20', '--reason', 'review', '--evidence', 'review')
        self.assertEqual(p.returncode, 2)
        self.assertTrue(json.loads(p.stdout)['stale'])
        rows = self.history()
        rows.append(dict(week_of='2026-10-05', actual_km=5))
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        p = self.cli('progression', '--log', str(self.log), '--as-of', '2026-10-07')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('partial', p.stderr)

    def test_load_chart_labels_are_not_injury_predictions(self):
        text = (ROOT/'skill/scripts/charts.py').read_text()
        self.assertNotIn('"high risk"', text)
        self.assertIn('Observed training load', text)


    def test_recent_unusable_run_does_not_make_old_efficiency_fresh(self):
        rows = [dict(id=str(i), date=f'2026-08-{i:02}', sport='Run', moving_s=1800,
                     pct_above_ceiling=5, ef=1.2) for i in range(1, 7)]
        rows.append(dict(id='latest', date='2026-10-04', sport='Run', moving_s=1800))
        (self.home/'athlete/state/run_log.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        p = self.cli('efficiency', '--as-of', '2026-10-05')
        out = json.loads(p.stdout)
        self.assertTrue(out['quality']['usable_stale'])
        self.assertEqual(out['quality']['confidence'], 'low')
        self.assertNotIn('change_pct', out)

    def test_gap_in_weekly_coverage_requires_review(self):
        rows = self.history()
        del rows[1]
        rows.insert(0, dict(week_of='2026-08-31', actual_km=31, week_kind='normal',
                            tolerated=True, coverage_complete=True))
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        p, out = self.review(mode='planned-down', proposed='25')
        self.assertEqual(p.returncode, 2)
        self.assertIsNone(out['max_next_week_km'])

    def test_missing_latest_completed_week_has_no_allowance(self):
        rows = self.history(last=31)
        rows[-1]['week_kind'] = 'normal'
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        args = ('progression', '--log', str(self.log), '--as-of', '2026-10-12',
                '--mode', 'build', '--proposed-km', '32', '--reason', 'review',
                '--evidence', 'synthetic completed history')
        p = self.cli(*args)
        out = json.loads(p.stdout)
        self.assertEqual(p.returncode, 2)
        self.assertFalse(out['accepted'])
        self.assertIsNone(out['max_next_week_km'])
        self.assertIn('latest completed week', ' '.join(out['reasons']))
        rows.append(dict(week_of='2026-10-05', actual_km=31, week_kind='normal',
                         tolerated=True, coverage_complete=True))
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        p = self.cli(*args)
        out = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(out['accepted'])
        self.assertEqual(out['max_next_week_km'], 34.1)

    def test_missing_reason_or_evidence_cannot_validate_return(self):
        self.history()
        p = self.cli('progression', '--log', str(self.log), '--as-of', '2026-10-05',
                     '--mode', 'planned-down', '--proposed-km', '20')
        self.assertEqual(p.returncode, 2)
        self.assertIn('explicit reason and evidence', ' '.join(json.loads(p.stdout)['reasons']))


    def test_stream_verdict_is_observation_not_diagnosis(self):
        st = dict(time=list(range(0, 3601, 60)), heart_rate=[140]*61,
                  velocity_smooth=[3]*61, watts=[200]*61)
        p = self.cli('streams', '--streams', json.dumps(st))
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertIn('not a fitness diagnosis', out['limitations'])
        self.assertEqual(out['quality']['hr_missing_s'], 0)


    def test_validator_does_not_accept_more_than_its_displayed_cap(self):
        rows = self.history(last=0)
        rows[-1]['week_kind'] = 'travel'
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        p, out = self.review(mode='travel-return', proposed='23.24')
        self.assertEqual(p.returncode, 2)
        self.assertLessEqual(out['max_next_week_km'], 23.2)

    def test_rejected_nonfinite_proposal_is_valid_json(self):
        self.history()
        p = self.cli('progression', '--log', str(self.log), '--as-of', '2026-10-05',
                     '--mode', 'planned-down', '--proposed-km', 'nan',
                     '--reason', 'review', '--evidence', 'review')
        def fail_constant(value):
            raise ValueError(value)
        out = json.loads(p.stdout, parse_constant=fail_constant)
        self.assertFalse(out['accepted'])


    def test_repeated_missed_targets_do_not_contradict_hold_advice(self):
        rows = self.history(last=31)
        for r in rows:
            r.update(week_kind='normal', target_km=45, pct_of_target=69)
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        p, out = self.review(proposed='32')
        self.assertEqual(p.returncode, 2)
        self.assertEqual(out['max_next_week_km'], 31)


if __name__ == '__main__':
    unittest.main()
