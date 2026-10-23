import os
import sys

from . import config, render


def main(argv):
    if len(argv) != 4 or argv[1] != "render":
        print("usage: python -m avantp render <config.json> <outdir>", file=sys.stderr)
        return 2
    try:
        cfg = config.load(argv[2])
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    for rel, body in render.render_all(cfg).items():
        p = os.path.join(argv[3], rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(body)
        print(p)
    return 0


sys.exit(main(sys.argv))
