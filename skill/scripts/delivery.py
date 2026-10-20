#!/usr/bin/env python3
"""Durable scheduled delivery and per-activity queue. Standard library only."""
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

import coachconf as cc


def database():
    cc.STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(cc.STATE / 'delivery.sqlite3', timeout=30)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, sport TEXT, status TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS job (id TEXT PRIMARY KEY, message TEXT, next_chunk INTEGER, status TEXT, staged TEXT)')
    return db


def detect(db, raw):
    match = re.search(r'\[.*\]', raw, re.S)
    acts = json.loads(match.group() if match else raw)
    if not isinstance(acts, list) or any(not isinstance(a, dict) or
            not str(a.get('id', '')).isdigit() for a in acts):
        raise ValueError('invalid activity list')
    seen_path = cc.STATE / 'postrun_seen.txt'
    first = db.execute("SELECT value FROM meta WHERE key='initialized'").fetchone() is None
    legacy = set(seen_path.read_text().split()) if seen_path.exists() else set()
    seed = first and not seen_path.exists()
    with db:
        for aid in legacy:
            db.execute('INSERT OR IGNORE INTO activity VALUES (?, ?, ?)', (aid, 'unknown', 'seeded'))
        for a in acts:
            aid, sport = str(a['id']), str(a.get('sport_type', 'unknown'))
            fresh = False
            try:
                start = datetime.fromisoformat(str(a.get('start_local', '')))
                now = datetime.now(start.tzinfo)
                fresh = timedelta(0) <= now - start < timedelta(hours=12)
            except ValueError:
                pass
            status = 'seeded' if seed else ('pending' if fresh and sport not in
                     (cc.get('activities.commute_types') or []) else 'ignored')
            db.execute('INSERT OR IGNORE INTO activity VALUES (?, ?, ?)', (aid, sport, status))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('initialized', '1')")
    # Compatibility only: the database, not this discovery list, owns delivery status.
    ids = [r[0] for r in db.execute('SELECT id FROM activity ORDER BY rowid')]
    tmp = seen_path.with_suffix('.tmp')
    tmp.write_text('\n'.join(ids) + '\n')
    tmp.replace(seen_path)


def covered_ids():
    ids = set()
    if cc.CHECKIN_LOG.exists():
        for line in cc.CHECKIN_LOG.read_text().splitlines():
            try:
                entry = json.loads(line)
                ids.update(str(i) for i in entry.get('activity_ids', []))
            except (ValueError, AttributeError, TypeError):
                continue
    return ids


def record_detect(db, ok):
    row = db.execute("SELECT value FROM meta WHERE key='detect_failures'").fetchone()
    n = int(row[0]) if row else 0
    text = None
    if ok and n >= 4:
        text = 'Post-run reactions are working again.'
    elif not ok and n == 3:
        text = 'Post-run detection failed four consecutive polls. Check logs/postrun.log.'
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('detect_failures', ?)", (str(0 if ok else n+1),))
    if text and os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'):
        try:
            telegram('sendMessage', ['--data-urlencode', 'chat_id=' + os.environ['TELEGRAM_CHAT_ID'],
                                    '--data-urlencode', 'text=' + text])
        except (OSError, RuntimeError) as exc:
            print(f'Detection alert failed: {exc}', file=sys.stderr)


def watch():
    cc.STATE.mkdir(parents=True, exist_ok=True)
    with (cc.STATE / 'postrun.poll.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        db = database()
        try:
            cmd = [os.environ['CLAUDE_BIN'], '--model', 'haiku', '--strict-mcp-config',
                   '--mcp-config', '{"mcpServers":{"strava-mcp":{"type":"http","url":"https://mcp.strava.com/mcp"}}}',
                   '--allowedTools', 'mcp__strava-mcp', '-p',
                   'Call strava-mcp list_activities with per_page=100. Output ONLY a JSON array '
                   'with id, sport_type, start_local (ISO timestamp) for every returned activity.']
            result = subprocess.run(cmd, cwd=cc.HOME, capture_output=True, text=True, timeout=300)
            if result.returncode:
                raise ValueError('activity detection failed')
            detect(db, result.stdout)
            record_detect(db, True)
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            print(str(exc), file=sys.stderr)
            record_detect(db, False)
        failed, attempted = recover(db)
        for aid, sport in db.execute("SELECT id, sport FROM activity WHERE status='pending' ORDER BY rowid").fetchall():
            if 'postrun:' + aid in attempted:
                status = db.execute('SELECT status FROM job WHERE id=?', ('postrun:' + aid,)).fetchone()[0]
                with db:
                    db.execute('UPDATE activity SET status=? WHERE id=?', (status, aid))
                continue
            if aid in covered_ids():
                with db:
                    db.execute("UPDATE activity SET status='covered' WHERE id=?", (aid,))
                continue
            result = subprocess.run([str(cc.SCRIPTS / 'check_in.sh'), 'postrun', aid, sport], cwd=cc.HOME)
            if result.returncode == 0:
                status = db.execute('SELECT status FROM job WHERE id=?', ('postrun:' + aid,)).fetchone()[0]
                with db:
                    db.execute('UPDATE activity SET status=? WHERE id=?', (status, aid))
            else:
                failed = True
        db.close()
        return int(failed)


def telegram(method, args):
    """Curl must succeed AND Telegram must acknowledge ok=true."""
    cmd = [os.environ.get('COACH_CURL_BIN', 'curl'), '--silent', '--show-error',
           '--fail-with-body', '--connect-timeout', '10', '--max-time', '30',
           '-X', 'POST', 'https://api.telegram.org/bot' +
           os.environ['TELEGRAM_BOT_TOKEN'] + '/' + method, *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        body = json.loads(result.stdout)
        if result.returncode or not isinstance(body, dict) or body.get('ok') is not True:
            raise ValueError('Telegram rejected delivery')
        return body
    except (ValueError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Telegram delivery not confirmed') from exc


def append_log(path, entries):
    """Serialize appenders and deduplicate recovery commits by stable entry id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        out.seek(0)
        existing = set()
        terminated = True
        for line in out:
            terminated = line.endswith('\n')
            try:
                existing.add(json.loads(line).get('delivery_id'))
            except (ValueError, AttributeError):
                continue
        out.seek(0, 2)
        if not terminated:
            out.write('\n')  # Isolate an interrupted append; recovery writes a complete entry.
        for entry in entries:
            token = entry.get('delivery_id')
            if token and token in existing:
                continue
            out.write(json.dumps(entry, ensure_ascii=False) + '\n')
            existing.add(token)
        out.flush()
        os.fsync(out.fileno())


def generate(prompt):
    cmd = [os.environ['CLAUDE_BIN'], '--settings',
           '{"enabledPlugins":{"telegram@claude-plugins-official":false}}', '-p', prompt]
    with tempfile.TemporaryDirectory(prefix='coach-generation-') as stage:
        stage_path = Path(stage) / 'coverage.jsonl'
        env = dict(os.environ, COACH_DELIVERY_STAGE=str(stage_path))
        result = subprocess.run(cmd, cwd=cc.HOME, capture_output=True, text=True, timeout=900, env=env)
        staged = [json.loads(line) for line in stage_path.read_text().splitlines()] if stage_path.exists() else []
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result, staged


def checkin(mode, aid, sport, no_send, prompt):
    if no_send:
        result, _ = generate(prompt)
        return result.returncode or (0 if result.stdout.strip() else 65)
    cc.STATE.mkdir(parents=True, exist_ok=True)
    with (cc.STATE / 'delivery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db = database()
        key = 'postrun:' + aid if mode == 'postrun' else mode + ':' + datetime.now().date().isoformat()
        row = db.execute('SELECT message, next_chunk, status, staged FROM job WHERE id=?', (key,)).fetchone()
        if row and row[2] in ('delivered', 'skipped', 'covered'):
            return 0
        if not row:
            if mode == 'postrun' and aid in covered_ids():
                with db:
                    db.execute('INSERT INTO job VALUES (?, ?, 0, ?, ?)', (key, '', 'covered', '[]'))
                return 0
            generation_timeout = None
            try:
                result, staged = generate(prompt)
            except subprocess.TimeoutExpired as exc:
                generation_timeout = exc
                stdout = exc.stdout or ''
                stderr = exc.stderr or ''
                result = subprocess.CompletedProcess(exc.cmd, 124,
                    stdout.decode('utf-8', errors='replace') if isinstance(stdout, bytes) else stdout,
                    stderr.decode('utf-8', errors='replace') if isinstance(stderr, bytes) else stderr)
                staged = []
            if result.returncode or not result.stdout.strip():
                cc.OUTBOX.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', prefix=key.replace(':', '-') + '-',
                                                 suffix='.txt', dir=cc.OUTBOX, delete=False) as out:
                    out.write(result.stdout + '\n--- stderr ---\n' + result.stderr)
                    out.flush()
                    os.fsync(out.fileno())
                if mode in ('morning', 'evening') and cc.get('integrations.telegram') is True:
                    try:
                        telegram('sendMessage', ['--data-urlencode', 'chat_id=' + os.environ['TELEGRAM_CHAT_ID'],
                                                '--data-urlencode', 'text=' + mode + ' generation failed; check outbox diagnostics.'])
                    except (OSError, RuntimeError) as exc:
                        print(f'Generation alert failed: {exc}', file=sys.stderr)
                if generation_timeout is not None:
                    raise generation_timeout
                return result.returncode or 65
            message = result.stdout.strip()
            with db:
                db.execute('INSERT OR REPLACE INTO job VALUES (?, ?, 0, ?, ?)', (key, message, 'pending', json.dumps(staged)))
            row = (message, 0, 'pending', json.dumps(staged))
        return deliver_job(db, key, row)


def deliver_job(db, key, row):
    message, next_chunk, status, staged_json = row
    if status in ('delivered', 'skipped', 'covered'):
        return 0
    if key.startswith('postrun:') and message.strip() == 'SKIP':
        with db:
            db.execute("UPDATE job SET status='skipped' WHERE id=?", (key,))
        return 0
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    for i in range(next_chunk, len(chunks)):
        telegram('sendMessage', ['--data-urlencode', 'chat_id=' + os.environ['TELEGRAM_CHAT_ID'],
                                '--data-urlencode', 'text=' + chunks[i]])
        with db:
            db.execute('UPDATE job SET next_chunk=? WHERE id=?', (i+1, key))
    entries = json.loads(staged_json)
    for i, entry in enumerate(entries):
        entry['delivery_id'] = key + ':' + str(i)
    if entries:
        append_log(cc.CHECKIN_LOG, entries)
    with db:
        db.execute("UPDATE job SET status='delivered' WHERE id=?", (key,))
    return 0


def recover(db):
    failed, attempted = False, set()
    if cc.get('integrations.telegram') is not True:
        return failed, attempted
    with (cc.STATE / 'delivery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = db.execute("SELECT id, message, next_chunk, status, staged FROM job WHERE status='pending'").fetchall()
        for key, *row in rows:
            attempted.add(key)
            try:
                deliver_job(db, key, row)
            except (OSError, RuntimeError) as exc:
                print(f'Outbox retry failed ({key}): {exc}', file=sys.stderr)
                failed = True
    return failed, attempted


def main():
    try:
        if sys.argv[1] == 'watch':
            return watch()
        if sys.argv[1] == 'checkin':
            mode, aid, sport, dry = sys.argv[2:6]
            return checkin(mode, aid, sport, dry == '1', sys.stdin.read())
        if sys.argv[1] == 'retry':
            db = database()
            try:
                failed, _ = recover(db)
                return int(failed)
            finally:
                db.close()
        if sys.argv[1] == 'telegram':
            telegram(sys.argv[2], sys.argv[3:])
            return 0
        raise ValueError('unknown delivery command')
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print('Delivery failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
