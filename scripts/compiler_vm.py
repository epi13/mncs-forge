#!/usr/bin/env python3
"""Invoke selected canonical MNCS work through Forge's retained application."""
import argparse
import json
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mncs_forge.compiler_vm import CanonicalVmApplication


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--calls', required=True)
    parser.add_argument('--library', action='append', default=[])
    parser.add_argument('--cache', required=True)
    args = parser.parse_args(argv)
    app = None
    try:
        app = CanonicalVmApplication(compiler_checkout=Path(os.environ['MNCS_COMPILER_CHECKOUT']),
            compiler_executable=Path(os.environ['MNCS_COMPILER_PROBE']), vm_checkout=Path(os.environ['MNCS_VM_CHECKOUT']),
            executable=Path(os.environ['MNCS_VM_BIN']), cache=Path(args.cache))
        results = [app.call(Path(args.source), call, libraries=args.library) for call in json.loads(Path(args.calls).read_text())]
        print(json.dumps({'schema_version':'mncs.forge.canonical-execution/1','results':results,'retained':app.status()},sort_keys=True))
        return 0
    except (OSError, KeyError, ValueError, RuntimeError) as error:
        print(json.dumps({'schema_version':'mncs.forge.canonical-execution/1','status':'unknown','reason':str(error)})); return 2
    finally:
        if app: app.close()


if __name__ == '__main__':
    raise SystemExit(main())
