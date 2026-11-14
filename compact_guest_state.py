"""Archive redundant legacy histories only after verifying every turn in Hermes.

Run with the sidecar stopped. Hermes SQLite is opened read-only. No session,
anchor or compaction data is removed from Hermes.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import sqlite3


def compact(state_path: Path, hermes_db: Path, apply: bool = False) -> dict:
    original = state_path.read_bytes()
    state = json.loads(original)
    if state.get('pending_jobs'):
        raise RuntimeError('Sidecar must be idle before state compaction')
    removed = {}
    retained = 0
    with sqlite3.connect(hermes_db.resolve().as_uri() + '?mode=ro', uri=True) as db:
        for sid, entry in state.get('reply_sessions', {}).items():
            legacy = entry.get('messages') or []
            if not legacy:
                continue
            native = iter(db.execute('SELECT role, content FROM messages WHERE session_id=? ORDER BY id', (sid,)))
            verified = all(any(row == (turn.get('role'), turn.get('content')) for row in native) for turn in legacy)
            if not verified:
                retained += 1
                continue
            removed[sid] = legacy
            entry.update(messages=[], native=True)
    report = {'verified_sessions': len(removed), 'retained_sessions': retained, 'before_bytes': len(original)}
    result = (json.dumps(state, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    report['after_bytes'] = len(result)
    if apply:
        if state_path.read_bytes() != original:
            raise RuntimeError('State changed during verification; no write performed')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        archive = state_path.with_name('legacy-history-' + stamp + '.json')
        # Preserve the entire original state, not just the histories.
        with open(os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(original); stream.flush(); os.fsync(stream.fileno())
        tmp = state_path.with_name('.state-compact-' + stamp)
        try:
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                stream.write(result); stream.flush(); os.fsync(stream.fileno())
            if state_path.read_bytes() != original:
                raise RuntimeError('State changed before replacement; original retained')
            tmp.replace(state_path)
        finally:
            tmp.unlink(missing_ok=True)
        report['archive'] = str(archive)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--hermes-db', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(compact(args.state, args.hermes_db, args.apply)))
