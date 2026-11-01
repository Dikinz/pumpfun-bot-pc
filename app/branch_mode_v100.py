"""Optional AMM scenario. Existing open AMM capital must never be abandoned."""
from pathlib import Path
from contextlib import closing
import sqlite3

def modules(primary):
    import config,paper_branch
    if getattr(config,'AMM_BRANCH_ENABLED',False):return ('paper_branch','route_branch')
    target=paper_branch.path(primary)
    if target.exists():
        with closing(sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as c:
            if c.execute('SELECT 1 FROM positions LIMIT 1').fetchone():
                raise RuntimeError('AMM_DISABLED_WITH_OPEN_POSITIONS: preserve the campaign; set AMM_BRANCH_ENABLED=1 to manage existing AMM positions before disabling it')
    return ('route_branch',)
