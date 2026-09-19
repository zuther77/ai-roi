# filler-pool

The rotating set of pre-made tracks the stream plays (design spec Section 3.6).

Today it is the only source of audio. From Sprint 3 onward it becomes the
fallback that plays whenever the live queue has nothing ready — the thing that
makes "the stream never stops" (Section 1.3) true rather than aspirational.

Everything in this directory except this README is gitignored.

```
filler-pool/
  audio/              <- put your tracks here
  filler.db           <- SQLite metadata, created automatically
  current_track.txt   <- path of whatever is playing right now
```

## Adding tracks

Drop audio files into `filler-pool/audio/` and restart the container:

```sh
docker compose restart playout
```

The controller reconciles the database against the directory on every start:
new files are added with their duration measured by `ffprobe`, and rows whose
file has disappeared are deleted. No manual database work is needed.

Recognised extensions are `.mp3`, `.wav`, `.flac`, `.m4a`, `.aac`, `.ogg` and
`.opus`. Anything else is ignored rather than handed to `ffprobe`.

**Use 5 or more tracks.** Day 2's acceptance criterion about no track playing
twice in a row is specified for a pool of at least 5, and the controller logs
a warning below that. The selection rule still works with fewer — it shrinks
its exclusion window to fit — but a 1-track pool has no choice but to repeat.

**Licensing.** Creative Commons or your own recordings only, nothing
copyrighted, even for a private test. This is a public repository and the
output is broadcast to YouTube, where Content ID applies (design spec edge
case #21).

## Inspecting state

What is playing right now:

```sh
cat filler-pool/current_track.txt
```

Play counts and rotation history, which is how you verify the no-repeat rule:

```sh
docker compose run --rm playout python -c "
from filler_pool import FillerPool
from pathlib import Path
pool = FillerPool(Path('/app/filler-pool/filler.db'), Path('/app/filler-pool/audio'))
for t in pool.all_tracks():
    print(f'{t.play_count:4d} plays  last={t.last_played_at}  {Path(t.file_path).name}')
"
```
