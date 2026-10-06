"""Command line helpers. Run inside the container:

    docker exec -it homefinance python -m app.cli create-user --username paul
    python -m app.cli gen-key > hf_master_key
"""

from __future__ import annotations

import argparse
import getpass
import io
import sys

import qrcode

from .config import load_settings
from .crypto import generate_master_key


def _gen_key(_: argparse.Namespace) -> int:
    print(generate_master_key())
    return 0


def _create_user(args: argparse.Namespace) -> int:
    from .bootstrap import build_services

    services = build_services(load_settings())
    if services.auth.has_user():
        print("A user already exists. This server is built for one user.", file=sys.stderr)
        return 1
    password = getpass.getpass("Password (at least 12 characters): ")
    if password != getpass.getpass("Repeat password: "):
        print("The passwords do not match.", file=sys.stderr)
        return 1
    try:
        secret, uri, codes = services.auth.create_user(args.username, password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("\nUser created. Set up your authenticator app now.\n")
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    buffer = io.StringIO()
    qr.print_ascii(out=buffer, invert=True)
    print(buffer.getvalue())
    print(f"Can't scan? Enter this key by hand: {secret}")
    print("\nRecovery codes. Each works once if you lose the authenticator.")
    print("Store them in your password manager. They are not shown again.\n")
    for code in codes:
        print(f"  {code}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gen-key", help="print a new 256-bit master key").set_defaults(func=_gen_key)
    create = sub.add_parser("create-user", help="create the login (password + authenticator)")
    create.add_argument("--username", required=True)
    create.set_defaults(func=_create_user)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
