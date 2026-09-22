# worker/ — DELL generation worker

Day 4 of the detailed plan: the job-queue skeleton that lets the master and
the DELL laptop exchange work over the direct Ethernet link.

## What's here

- `dell_worker.py` — the worker skeleton. Connects to the master's Redis,
  atomically claims jobs with `BRPOPLPUSH jobs:pending jobs:in_progress`,
  logs the claim, then acknowledges by removing the job from
  `jobs:in_progress`. Stdlib-only (a ~100-line RESP2 client inside), so it
  runs on bare Windows + Python with zero pip installs. No generation yet.
- `push_test_job.sh` — runs **on the master**. Pushes one fake job
  (`prompt: "test"`) onto `jobs:pending` via redis-cli inside the Redis
  container. The password never appears in any argv.

## Job schema (Day 4, task 4)

```json
{"job_id": "uuid", "prompt": "string", "target_duration_sec": 30,
 "priority": "live | filler", "created_at": "iso8601"}
```

## Running the worker on DELL

Requires only Python 3.10+ on the DELL box and the link to the master up
(`ping 192.168.50.1`). Get `REDIS_PASSWORD` from the master's `.env`.

Windows cmd:

```bat
set REDIS_URL=redis://192.168.50.1:6379/0
set REDIS_PASSWORD=<value from master .env>
python dell_worker.py
```

Both variables can also be baked into the environment permanently; the
password must be set or startup exits with `worker_start_error`.

## Deliberately not here yet

- Real generation (ACE-Step) and containerizing this worker — Day 5.
- Recovery of jobs left in `jobs:in_progress` after a worker crash, and any
  persistence in Redis — Day 6. For now a crashed worker's claimed job
  simply stays visible in `jobs:in_progress` and is **not** re-claimed by
  anyone (that's the atomicity Day 4 asks to prove, not a bug).
- NFS track writing, health heartbeat — later in Sprint 2.
