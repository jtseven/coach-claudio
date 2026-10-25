#!/usr/bin/env python3
"""Explicit local ledger -> YAZIO outbox. No hooks and no network on import/status."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import uuid

import nutrition_core as core
import nutrition_yazio as ny


class Sync:
    def __init__(self, directory, client_factory=None, enabled=False, dry_run=False):
        self.directory = Path(directory)
        self.path = self.directory / 'sync_outbox.sqlite3'
        self.client_factory = client_factory or ny.Client.load
        self.enabled, self.dry_run = enabled, dry_run

    def latest(self):
        path = self.directory / 'ledger.sqlite3'
        if not path.exists():
            return []
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(
                'SELECT e.id,e.rev,e.state,r.payload FROM entries e JOIN revisions r '
                'ON r.entry_id=e.id AND r.rev=e.rev ORDER BY e.id')]

    def status(self):
        rows = []
        if self.path.exists():
            with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = [dict(r) for r in db.execute('SELECT * FROM jobs')]
        counts = dict.fromkeys(('pending', 'synced', 'blocked', 'unknown'), 0)
        details = []
        for entry in self.latest():
            jobs = [j for j in rows if j['entry_id'] == entry['id']]
            current = [j for j in jobs if j['rev'] == entry['rev']]
            outstanding = [j for j in jobs if j['phase'] != 'retired' and
                           (j['rev'] != entry['rev'] or entry['state'] != 'active')]
            relevant = current + outstanding
            if any(j['state'] == 'unknown' for j in relevant):
                state = 'unknown'
            elif any(j['state'] == 'blocked' for j in relevant):
                state = 'blocked'
            elif outstanding:
                state = 'pending'
            elif entry['state'] != 'active':
                state = 'synced'
            elif current and all(j['phase'] == 'done' for j in current):
                state = 'synced'
            else:
                state = 'pending'
            counts[state] += 1
            details.append({'entry_id': entry['id'], 'rev': entry['rev'], 'state': state,
                            'reasons': sorted({j['reason'] for j in relevant if j['reason']})})
        return {**counts, 'enabled': self.enabled, 'dry_run': self.dry_run, 'entries': details}

    @contextmanager
    def locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        ny._check_private(self.directory, True)
        lock = self.path.with_suffix('.lock')
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            ny._check_private(lock, False)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not self.path.exists():
                out = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(out)
            ny._check_private(self.path, False)
            with sqlite3.connect(self.path, isolation_level=None) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA secure_delete=ON')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('CREATE TABLE IF NOT EXISTS jobs ('
                           'entry_id TEXT, rev INTEGER, item INTEGER, snapshot TEXT, '
                           'pid TEXT, cid TEXT, product TEXT, consumed TEXT, day TEXT, '
                           'phase TEXT, state TEXT, reason TEXT, '
                           'PRIMARY KEY(entry_id,rev,item))')
                yield db
        finally:
            os.close(fd)

    def update(self, db, job, **fields):
        db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in fields) +
                   ' WHERE entry_id=? AND rev=? AND item=?',
                   (*fields.values(), job['entry_id'], job['rev'], job['item']))
        job.update(fields)

    def enqueue(self, db, entries):
        # One durable transaction for all item UUIDs, before loading any credentials.
        db.execute('BEGIN IMMEDIATE')
        try:
            for e in entries:
                if e['state'] != 'active':
                    continue
                inp = json.loads(e['payload'])['input']
                for i, item in enumerate(inp['items']):
                    snapshot = json.dumps({'item': item, 'when': inp['eaten_at'], 'meal': inp['meal']})
                    db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,NULL,NULL,NULL,?,?,?)',
                               (e['id'], e['rev'], i, snapshot, str(uuid.uuid4()), str(uuid.uuid4()),
                                'product', 'pending', ''))
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise

    def prepare(self, client, job):
        snap = json.loads(job['snapshot'])
        raw = snap['item']
        per, _ = core.parse_basis(raw.get('basis'))
        if per not in ('100g', '100ml', 'total'):
            raise ValueError('unsupported basis: expected 100g, 100ml or total with explicit g/ml quantity')
        if not raw.get('amount'):
            raise ValueError('missing amount: measured or explicitly estimated g/ml quantity required')
        computed = core.compute_item(raw)
        quantity, unit = core.parse_quantity(raw['amount'])
        dimension = core.dimension(unit)
        if dimension == 'count':
            raise ValueError('unsupported amount: explicit g/ml quantity required (no guessed serving)')
        unit = 'g' if dimension == 'mass' else 'ml'
        amount = float(quantity * (core.MASS_G if unit == 'g' else core.VOLUME_ML)[core.parse_quantity(raw['amount'])[1]])
        if per != 'total' and per != '100' + unit:
            raise ValueError('amount/basis dimension mismatch: no implicit ml-to-g conversion')
        caps = client.capabilities()
        for op in ('create_private_product', 'create_consumed'):
            if caps.get(op) != 'live_confirmed':
                raise ny.ContractUnverified(op + ' is not live_confirmed; run nutrition_yazio.py verify-write')
        if caps.get('base_unit') != unit:
            raise ny.ContractUnverified('base unit ' + unit + ' is not live_confirmed (no assumed density)')
        nutrients, trace = core.parse_nutrients(raw.get('nutrients'), [])
        if trace:
            raise ValueError('qualified/trace nutrients cannot be exported as exact values: ' + ', '.join(trace))
        factor = 100 / amount if per == 'total' else 1
        nutrients = {k: None if v is None else float(v) * factor for k, v in nutrients.items()}
        product = client.prepare_product('100' + unit, nutrients, job['pid'], computed['name'], raw.get('brand'))
        if product.get('local_only'):
            raise ny.ContractUnverified('known nutrients not live-confirmed: ' + ', '.join(product['local_only']))
        consumed = client.prepare_consumed(job['cid'], job['pid'], amount, unit,
                                          datetime.fromisoformat(snap['when']),
                                          snap['meal'] if snap['meal'] in ny.DAYTIMES else 'snack', unit)
        if consumed.get('warnings'):
            raise ValueError('; '.join(consumed['warnings']))
        return json.dumps(product['payload']), json.dumps(consumed['payload']), consumed['day']

    def advance(self, db, client, job):
        if not job['product']:
            product, consumed, day = self.prepare(client, job)
            self.update(db, job, product=product, consumed=consumed, day=day)
        for phase, reconcile, create, payload, next_phase in (
            ('product', client.reconcile_product, client.create_private_product, job['product'], 'consumed'),
            ('consumed', client.reconcile_consumed, client.create_consumed, job['consumed'], 'done')):
            if job['phase'] != phase:
                continue
            draft = json.loads(payload)
            result = reconcile(draft)
            if result['status'] == 'absent':
                self.update(db, job, state='unknown', reason=phase + ' write in flight; reconcile before retry')
                result = create(draft)
            if result['status'] != 'confirmed':
                raise ny.ReadbackMismatch(phase, job['cid'], ['not confirmed by readback'])
            # Independently require readback, even for a misleading adapter return.
            if reconcile(draft)['status'] != 'confirmed':
                raise ny.UnknownOutcome(phase, job['cid'], job['day'], 'readback not confirmed')
            self.update(db, job, phase=next_phase, state='synced' if next_phase == 'done' else 'pending', reason='')

    def retire(self, db, client, job):
        # Superseded in-flight creates must be reconciled before discarding their ids.
        if job['state'] == 'unknown' and job['phase'] in ('product', 'consumed'):
            payload = job[job['phase']]
            reconcile = client.reconcile_product if job['phase'] == 'product' else client.reconcile_consumed
            if payload and reconcile(json.loads(payload))['status'] not in ('absent', 'confirmed'):
                raise ny.UnknownOutcome('reconcile superseded', job['cid'], job['day'])
        if job['day']:
            if client.capabilities().get('delete_consumed') != 'live_confirmed':
                raise ny.ContractUnverified('delete_consumed is not live_confirmed; correction/deletion blocked')
            self.update(db, job, phase='deleting', state='unknown', reason='deletion awaiting readback')
            exists = any(x['id'] == job['cid'] for x in client.get_consumed(job['day'])['products'])
            if exists:
                client.delete_consumed(job['cid'], job['day'])
            if any(x['id'] == job['cid'] for x in client.get_consumed(job['day'])['products']):
                raise ny.UnknownOutcome('delete_consumed', job['cid'], job['day'], 'still present')
        self.update(db, job, phase='retired', state='synced', reason='', snapshot=None, product=None, consumed=None)

    def run(self):
        if not self.enabled or self.dry_run or os.environ.get('COACH_DRY_RUN') == '1':
            return self.status()
        with self.locked() as db:
            entries = self.latest()
            self.enqueue(db, entries)
            # Without prepared drafts/day no write could have been attempted.
            # Erase/supersede those local-only snapshots even without credentials.
            for e in entries:
                db.execute("UPDATE jobs SET phase='retired',state='synced',reason='',"
                           "snapshot=NULL,product=NULL,consumed=NULL WHERE entry_id=? "
                           "AND (rev!=? OR ?!='active') AND day IS NULL",
                           (e['id'], e['rev'], e['state']))
            if not db.execute("SELECT 1 FROM jobs WHERE phase NOT IN ('done','retired') LIMIT 1").fetchone() and not any(
                db.execute("SELECT 1 FROM jobs WHERE entry_id=? AND phase='done' AND (rev!=? OR ?!='active')",
                           (e['id'], e['rev'], e['state'])).fetchone() for e in entries
            ):
                return self.status()
            try:
                client = self.client_factory()
            except ny.YazioError as exc:
                for e in entries:
                    db.execute("UPDATE jobs SET state='blocked',reason=? WHERE entry_id=? "
                               "AND phase!='retired' AND state!='unknown' "
                               "AND (phase!='done' OR rev!=? OR ?!='active')",
                               (str(exc), e['id'], e['rev'], e['state']))
                return self.status()
            for e in entries:
                jobs = [dict(r) for r in db.execute('SELECT * FROM jobs WHERE entry_id=? ORDER BY rev,item', (e['id'],))]
                old = [j for j in jobs if (j['rev'] != e['rev'] or e['state'] != 'active') and j['phase'] != 'retired']
                failed = False
                for job in old + [j for j in jobs if j['rev'] == e['rev'] and e['state'] == 'active' and j['phase'] != 'done']:
                    try:
                        if job in old:
                            self.retire(db, client, job)
                        elif not failed:
                            self.advance(db, client, job)
                    except (ny.YazioError, ValueError) as exc:
                        unknown = isinstance(exc, (ny.UnknownOutcome, ny.TransportError, ny.ProtocolError, ny.ReadbackMismatch)) or job['state'] == 'unknown'
                        self.update(db, job, state='unknown' if unknown else 'blocked', reason=str(exc))
                        if job in old:
                            failed = True
                if e['state'] == 'erased' and not failed:
                    db.execute('UPDATE jobs SET snapshot=NULL,product=NULL,consumed=NULL,reason=\'\' WHERE entry_id=?', (e['id'],))
        return self.status()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('status', 'run'))
    args = parser.parse_args(argv)
    import coachconf as cc
    sync = Sync(cc.NUTRITION_DIR, enabled=cc.get('integrations.nutrition_log', False) is True and
                cc.get('integrations.nutrition_yazio', False) is True,
                dry_run=cc.DRY_RUN or os.environ.get('COACH_DRY_RUN') == '1')
    try:
        result = sync.run() if args.command == 'run' else sync.status()
        print(json.dumps(result, indent=2))
        return 1 if result['blocked'] or result['unknown'] or (args.command == 'run' and result['pending']) else 0
    except (OSError, sqlite3.Error, ValueError, ny.YazioError) as exc:
        print(json.dumps({'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
