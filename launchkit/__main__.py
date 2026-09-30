"""python -m launchkit <command> [--help]"""
import importlib
import sys

COMMANDS = {"new": "new", "golive": "golive", "supabase-redirect": "supabase_redirect", "check": "check"}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: python -m launchkit {{{','.join(COMMANDS)}}} [--help]", file=sys.stderr)
        return 2
    return importlib.import_module(f"launchkit.{COMMANDS[argv[0]]}").main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
