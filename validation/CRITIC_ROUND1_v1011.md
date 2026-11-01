# Independent critic — round 1

Verdict: CHANGES_REQUIRED. Scope: readiness for the next Windows PAPER test only.

Frozen source: build/pumpfun_bot_v0.7.101.1_agent_review.

## Confirmed blocking defect: unknown admission counter produces false PASS

app/paper_fault_test_v101.py:21 sums an empty fallback endpoint list as zero, including when snapshot status is UNAVAILABLE. The observation at the fault window end becomes 0, while both before and after remain 734. The test reports resumed=true and passed=true without any new admission. Independently reproduced with agent_reviews/root/repro_counter_uncertainty.py. Preserve unknown observations explicitly; require reliable comparable counters for PASS. Include an unavailable-midpoint regression and unavailable-before/after cases.

## Confirmed status/evidence defect: late denial bypasses outer snapshot normalization

app/rpc_budget.py:668 catches OutcomeReadBusy in the internal _snapshot handler. A denial beginning at the third enumeration (or second when already blocked) returns UNAVAILABLE and pending_outcomes=0, drops the failure chain, and can overwrite BLOCKED. Evidence: probe_snapshot.py; critic_snapshot_before.xml has two expected failures. Regression source: test_review_snapshot_v1011.py. Send all read errors through the single outer snapshot handler, retain BLOCKED priority, pending_outcomes=None and reason/failure_chain.

## Checks independently completed

- Existing ten new regressions: 10 passed, critic_regressions.xml.
- Release guard hash verification: all 359 manifest entries verified.
- Reviewed RPC admission, durable outcomes/receipts, audited recovery, priority leases, timer STOP, launcher shutdown, branch session plan, dashboard overview, persistent wallet sensitivity, roadmap/release/upgrade instructions and check-plan inclusion.
- The three earlier fixes address their reproduced paths. The first fix remains incomplete for late snapshot denial; the timer guard addresses early manual STOP, but does not solve unknown counter observations.
- No provider credentials, real market socket or external RPC used. Native Windows 3.14, physical two-hour provider run, current AMM exit and live readiness remain external gates.
- Full-suite QA is pending; no full-suite approval is claimed in this round.
