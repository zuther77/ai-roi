"""Insert one prompt into the live queue. The web form does not exist yet.

    docker compose run --rm queue-manager python /app/queue/insert_prompt.py "lo-fi hip hop"
"""

from __future__ import annotations

import json
import os
import sys

from store import connect


def main() -> int:
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        print("usage: insert_prompt.py \"prompt text\"", file=sys.stderr)
        return 2
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    item = connect(url).enqueue(text)
    print(json.dumps(item))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
