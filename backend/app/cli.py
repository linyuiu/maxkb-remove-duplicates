"""Optional manual database bootstrap; background work starts with the API."""

import argparse
from .initialize import initialize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["bootstrap"])
    parser.parse_args()
    initialize()


if __name__ == "__main__":
    main()
