#!/usr/bin/env python3
"""One relocatable Python entry for MCP, offline Hooks and the repository CLI."""
from pathlib import Path
import sys


def main():
    sys.dont_write_bytecode = True
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / 'src'))
    from seywo.entrypoint import dispatch
    return dispatch(sys.argv[1] if len(sys.argv) > 1 else '', sys.argv[2:], plugin_root=root)


if __name__ == '__main__':
    raise SystemExit(main())
