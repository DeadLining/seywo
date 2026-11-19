#!/usr/bin/env python3
"""One relocatable Python entry for MCP, offline Hooks and the repository CLI."""
import os
from pathlib import Path
import sys


def main():
    sys.dont_write_bytecode = True
    # Model self-invocations never enter lifecycle handling.
    if sys.argv[1:2] == ['--hook'] and os.environ.get('SEYWO_INTERNAL_MODEL') == '1':
        return 0
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / 'src'))
    os.environ['PLUGIN_ROOT'] = str(root)
    os.environ['PYTHONUTF8'] = '1'
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    # JSON/transcripts are UTF-8 regardless of a Windows console's legacy codec.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    if len(sys.argv) < 2 or sys.argv[1] not in {'--mcp', '--hook', '--cli'}:
        print('Seywo launcher requires --mcp, --hook or --cli.', file=sys.stderr)
        return 2
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == '--mcp':
        from seywo.plugin import main as entry
    elif mode == '--hook':
        from seywo.hook import main as entry
    else:
        from seywo.cli import main as entry
    return entry() if mode == "--hook" else entry(args)


if __name__ == '__main__':
    raise SystemExit(main())
