# Glossary

## Queue item

One listener request. It waits, gets generated, becomes ready to air, goes on air, then counts as played. When a slot is needed, this module checks the file on disk and returns a path. The radio plays that path. A file that is on disk but cannot be played is sent back for one new generation, behind every queue item that already exists. Songs already waiting air first. If that file is also broken, the queue item is dropped. A worker failure, a broken file, and a playback failure share one extra generation total. After that, the queue item is dropped. A missing file does not use that generation until it has been skipped in three rounds. Then it uses the extra generation if one remains. Otherwise it is dropped. If the worker fails and produces no file, the queue item is sent to the back for that extra generation. If playback fails while the radio stays up, the radio reports that failure and the same rule applies. A crash puts the song back to ready. The worker does not run again.

## Round

One trip through the queue items that are ready to air. A ready item whose file is missing is skipped for the rest of this round. It is offered again on the next round, after every other ready item has been played or skipped once.
