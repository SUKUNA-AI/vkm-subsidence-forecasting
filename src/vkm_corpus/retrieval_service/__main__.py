"""``python -m vkm_corpus.retrieval_service serve|health`` — works before the coordinator registers the CLI group."""
import argparse
import sys

from vkm_corpus.retrieval_service import cli


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="vkm-rx580-retrieval")
    sub = parser.add_subparsers(dest="group")
    cli.register(sub)
    args = parser.parse_args(["retrieval", *(sys.argv[1:] if argv is None else argv)])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
