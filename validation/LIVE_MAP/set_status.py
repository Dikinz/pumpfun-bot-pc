"""Update one stage of the live testing map.

Usage: python set_status.py <stage_key> <pending|running|pass|fail|changes> "<note>" [round]
Each stage is stored in stages/<key>.json; state.json is rebuilt from all stages.
"""
import json, os, sys, time, datetime, pathlib

ROOT = pathlib.Path(__file__).resolve().parent
STAGES = ROOT / 'stages'


def atomic_write(path, data):
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    for _ in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    tmp.unlink(missing_ok=True)


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    key, state, note = argv[0], argv[1], argv[2]
    rnd = int(argv[3]) if len(argv) > 3 else None
    STAGES.mkdir(exist_ok=True)
    now = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    path = STAGES / f'{key}.json'
    prev = {}
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            prev = {}
    entry = {'key': key, 'state': state, 'note': note, 'round': rnd,
             'started': prev.get('started') or now, 'updated': now,
             'history': (prev.get('history') or [])[-19:] + [{'t': now, 'state': state, 'note': note}]}
    atomic_write(path, entry)
    stages = {}
    for p in STAGES.glob('*.json'):
        try:
            stages[p.stem] = json.loads(p.read_text(encoding='utf-8'))
        except ValueError:
            pass
    atomic_write(ROOT / 'state.json', {'updated': now, 'stages': stages})
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
