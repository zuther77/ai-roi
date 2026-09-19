# test-assets

Scratch inputs for the Day 1 stream test. Everything in this directory except
this README is gitignored, so nothing here is committed.

The `playout` container mounts this directory read-only at
`/app/test-assets` and expects two files:

| File | What it is |
|---|---|
| `track.mp3` | The audio that gets looped forever |
| `image.jpg` | The still picture shown behind it |

You can point the container somewhere else by setting `AUDIO_FILE` or
`IMAGE_FILE` in `.env`, but the defaults above need no configuration.

## Generating throwaway placeholders

If you just want to prove the pipeline works, FFmpeg can synthesise both files
from nothing. This runs inside the container, so it needs no software on your
Mac, and the output is generated rather than downloaded, so there is no
licensing question at all.

The `-v` flag adds a second, writable mount, because the normal
`./test-assets` mount is read-only by design.

```sh
# A 30-second brown-noise bed (sounds like steady rainfall). Pleasant enough to
# leave running for the 15-minute test, and any dropout is obvious.
docker compose run --rm -v "$(pwd)/test-assets:/out" playout \
  ffmpeg -f lavfi -i "anoisesrc=color=brown:duration=30:sample_rate=44100:amplitude=0.3" \
         -ac 2 -c:a libmp3lame -b:a 192k -y /out/track.mp3

# A single dark 1280x720 frame.
docker compose run --rm -v "$(pwd)/test-assets:/out" playout \
  ffmpeg -f lavfi -i "color=c=0x0f1419:s=1280x720" -frames:v 1 -y /out/image.jpg
```

## Using real audio instead

Placeholders are fine for confirming that frames reach YouTube, but "audio is
audible and in sync with no dropouts" is easier to judge against real music.
Drop any track you have the rights to at `test-assets/track.mp3` and restart
the container — no rebuild needed, since the directory is mounted rather than
baked into the image.

Do not use anything copyrighted, even for a private test. Day 2 needs a pool of
5–10 Creative Commons or self-recorded tracks regardless, so sourcing those now
saves a step later.
