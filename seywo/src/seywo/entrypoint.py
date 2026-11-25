"""Dispatch a launcher mode. Only an actual MCP server enters the SDK environment."""
from contextlib import nullcontext
import os
import sys


def dispatch(mode, args, *, plugin_root, sdk=nullcontext):
    if mode == '--hook' and os.environ.get('SEYWO_INTERNAL_MODEL') == '1':
        return 0
    os.environ['PLUGIN_ROOT'] = str(plugin_root)
    os.environ['PYTHONUTF8'] = '1'
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    if mode == '--hook':
        from .hook import main
        return main()
    if mode == '--cli':
        from .cli import main
        return main(args, sdk=sdk)
    if mode == '--mcp':
        with sdk():
            from .plugin import main
            return main(args)
    print('Seywo launcher requires --mcp, --hook or --cli.', file=sys.stderr)
    return 2
