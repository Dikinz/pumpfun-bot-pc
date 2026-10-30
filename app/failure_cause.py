"""Read-only cause precedence: monitor failure before its transport consequence."""
def first_message(failure):
    if not isinstance(failure,dict):return None
    for row in (failure or {}).get('chain',[]):
        if isinstance(row,dict) and row.get('message'):return row['message']
    return None


def branch(lifecycle,transport):
    failure=lifecycle.get('failure_v81') or {}
    monitor=failure.get('monitor_state') or {}
    root=first_message(monitor.get('failure_v81'))
    wrapper=first_message(failure.get('exception'))
    secondary=(transport.get('failure') or {}).get('reason')
    return dict(primary_cause=root or wrapper or secondary or lifecycle.get('reason') or 'BRANCH_FAILED',
                monitor_cause=root,branch_cause=wrapper,transport_cause=secondary,
                source='MONITOR_FAILURE' if root else 'BRANCH_EXCEPTION' if wrapper else 'TRANSPORT_OR_LIFECYCLE')
