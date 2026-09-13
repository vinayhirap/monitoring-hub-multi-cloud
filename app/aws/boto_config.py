# app/aws/boto_config.py
"""
Shared boto3 client Config for every CloudWatch/Describe API call in this
app.

Problem this fixes: every boto3.client(...) call site in this codebase
(collector_direct.py, metric_catalog.py, sts.py) constructed clients with
no explicit retry configuration, which means botocore's LEGACY retry mode
-- 3 total attempts, short fixed backoff, no awareness of how hard this
app hammers GetMetricData across many accounts/regions every tiered
collection cycle (see app/collector/scheduler.py). Under real load (more
accounts, more regions, tighter polling tiers) a burst of Throttling /
RequestLimitExceeded responses either surfaces as a logged collector
error with the cycle skipped -- a gap in metric_history exactly like the
kind of silent data-loss the 2026-08-26 Mumbai RCA had to root-cause
after the fact -- or retries inefficiently and makes the throttling
worse.

STANDARD_RETRY uses botocore's "adaptive" retry mode, which is the part
that actually helps here: it doesn't just retry more, it throttles this
process's own OUTGOING request rate once it observes throttling
responses on a given client, so one busy account's calls back off
instead of hammering AWS at a fixed rate while also retrying.

Usage: every boto3.client("...", ...) call in this app should pass
config=STANDARD_RETRY. This does not change connection pooling or
timeouts -- only retry/backoff behavior. Kept as a single shared object
(not a factory function) because botocore Config objects are immutable
and safe to reuse across every client construction.
"""
from botocore.config import Config

STANDARD_RETRY = Config(
    retries={
        "max_attempts": 5,   # includes the initial attempt -- 4 retries on top
        "mode": "adaptive",  # client-side rate limiting + retry, not bare retry
    },
)
