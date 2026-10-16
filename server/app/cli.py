"""Command line helpers. Run inside the container:

    docker exec -it homefinance python -m app.cli create-user --username paul
    docker exec -it homefinance python -m app.cli login-log
    docker exec -it homefinance python -m app.cli unlock
    docker exec -it homefinance python -m app.cli reset-networth
    docker exec homefinance python -m app.cli quote-test XIU.TO
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


def _unlock(_: argparse.Namespace) -> int:
    from .bootstrap import build_services

    services = build_services(load_settings())
    try:
        if services.auth.unlock():
            print("Sign-in unlocked. The failed-attempt count is back to zero.")
        else:
            print("Sign-in was not locked. The failed-attempt count is back to zero.")
    finally:
        services.db.close()
    return 0


def _login_log(args: argparse.Namespace) -> int:
    from .bootstrap import build_services

    services = build_services(load_settings())
    try:
        data = services.auth.login_log(args.limit)
    finally:
        services.db.close()
    state = "LOCKED since " + data["locked_at"] if data["locked"] else "not locked"
    print(f"Sign-in is {state}. Failed in a row: {data['failed_streak']} of {data['max_failed_logins']}.\n")
    for e in data["events"]:
        times = f" x{e['count']}" if e["count"] > 1 else ""
        print(f"{e['at']}  {e['result']:<13}{times:<6} from {e['ip'] or '-'}  {e['origin'] or ''}  {e['device_name'] or ''}")
    return 0


def _quote_test(args: argparse.Namespace) -> int:
    """Ask Yahoo Finance for one symbol, to tell a blocked connection from a busy moment."""
    from .quotes import QuoteError, YahooProvider

    try:
        quote = YahooProvider().quote(args.symbol.upper())
    except QuoteError as exc:
        wait = f" It asked us to wait {int(exc.retry_after)} seconds." if exc.retry_after else ""
        print(f"{args.symbol.upper()}: {exc.kind}: {exc}.{wait}")
        return 1
    print(f"{quote.symbol}: {quote.price} {quote.currency} (last trade {quote.day}). Yahoo Finance is answering.")
    return 0


def _reset_networth(args: argparse.Namespace) -> int:
    from .bootstrap import build_services

    services = build_services(load_settings())
    try:
        count = services.db.one("SELECT count(*) AS n FROM networth")["n"]
        if not args.yes:
            answer = input(
                f"Delete all {count} saved daily net-worth points? This is the whole chart history, "
                "including any from real banks. Type yes: "
            )
            if answer.strip().lower() != "yes":
                print("Nothing deleted.")
                return 1
        services.db.run("DELETE FROM networth")
    finally:
        services.db.close()
    print(f"Deleted {count} net-worth points. The chart starts again with the next sync.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gen-key", help="print a new 256-bit master key").set_defaults(func=_gen_key)
    create = sub.add_parser("create-user", help="create the login (password + authenticator)")
    create.add_argument("--username", required=True)
    create.set_defaults(func=_create_user)
    sub.add_parser("unlock", help="unlock sign-in after too many failed attempts").set_defaults(func=_unlock)
    reset = sub.add_parser("reset-networth", help="delete the saved net-worth chart history (for example after removing sandbox banks)")
    reset.add_argument("--yes", action="store_true", help="do not ask")
    reset.set_defaults(func=_reset_networth)
    test = sub.add_parser("quote-test", help="ask Yahoo Finance for one price, to check live prices work from here")
    test.add_argument("symbol", nargs="?", default="XIU.TO")
    test.set_defaults(func=_quote_test)
    log = sub.add_parser("login-log", help="show recent sign-in attempts")
    log.add_argument("--limit", type=int, default=50)
    log.set_defaults(func=_login_log)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
