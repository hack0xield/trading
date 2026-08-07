#!/usr/bin/env python3
"""Run the scheduled signal jobs and deliver them to Telegram.

    scripts/run_signals.py --dry-run          # print what would be sent
    scripts/run_signals.py --send             # actually send
    scripts/run_signals.py --check            # verify the bot token only
    scripts/run_signals.py --job eurusd-heartbeat --send

**Dry run is the default.** Passing `--send` is the only way a message leaves
the machine, because a scheduler that can message people before you have read
its output is a way to spam strangers at 3am.

Exit codes, so a scheduler can alert on them:
    0  every job succeeded (and delivered, if sending)
    1  a job failed, or a delivery failed
    2  configuration problem — nothing ran
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signals import Notifier, load_config, mt5_available, run_all  # noqa: E402
from signals.strategies import describe_all  # noqa: E402
from signals.telegram import discover_chats  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute scheduled signals and send them to Telegram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", "-c", default="configs/signals.yaml")
    parser.add_argument("--job", help="run only this job")
    parser.add_argument("--send", action="store_true", help="actually deliver (default: dry run)")
    parser.add_argument("--dry-run", action="store_true", help="explicit no-send (the default)")
    parser.add_argument("--check", action="store_true", help="verify the bot token and exit")
    parser.add_argument("--chats", action="store_true",
                        help="list chat ids the bot has heard from, for signals.yaml")
    parser.add_argument("--list-strategies", action="store_true",
                        help="show signal strategies and their params")
    parser.add_argument("--quiet", "-q", action="store_true", help="only print problems")
    args = parser.parse_args(argv)

    if args.list_strategies:
        print(describe_all())
        return 0

    try:
        config = load_config(args.config)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    notifier = Notifier(token=config.resolve_token(), dry_run=not args.send)

    if args.chats:
        if not notifier.token:
            print(f"No bot token. Put one in {config.token_file}.", file=sys.stderr)
            return 2
        chats, error = discover_chats(notifier.token)
        if error:
            print(f"getUpdates failed: {error}", file=sys.stderr)
            return 1
        if not chats:
            print(
                "No chats yet — a bot can only message someone who contacted it first.\n"
                "\n"
                "  For a personal chat: open Telegram, find your bot, press Start.\n"
                "  For a group:         add the bot, then post any message in it.\n"
                "\n"
                "Then run this again."
            )
            return 1
        print(f"{'chat_id':>16}  {'type':<12} title")
        print("-" * 56)
        for chat in chats:
            print(f"{chat['id']:>16}  {chat['type']:<12} {chat['title']}")
        print("\nPaste the id(s) into configs/signals.yaml, quoted:\n")
        print("groups:\n  ops:\n    chat_ids: [\"" + chats[0]["id"] + "\"]")
        return 0

    if args.check:
        ok, detail = notifier.check()
        print(f"telegram token: {'OK — ' + detail if ok else 'FAILED — ' + detail}")
        available, why = mt5_available()
        print(f"metatrader:     {'available' if available else 'unavailable — ' + why}")
        print(f"groups:         {', '.join(sorted(config.groups)) or 'none'}")
        print(f"jobs:           {', '.join(j.name for j in config.jobs) or 'none'}")
        return 0 if ok else 1

    if args.send and not notifier.token:
        print(
            "--send given but no bot token. Put one in "
            f"{config.token_file} or set TELEGRAM_BOT_TOKEN.",
            file=sys.stderr,
        )
        return 2

    results = run_all(config, only=args.job)
    if not results:
        print(f"No enabled job matched {args.job!r}." if args.job else "No enabled jobs.")
        return 2

    failures = 0
    for result in results:
        chat_ids = config.chat_ids_for(result.recipients)
        deliveries = notifier.send(chat_ids, result.message)

        if not result.ok:
            failures += 1
        if not args.quiet:
            head = "FAILED" if not result.ok else "ok"
            print(f"=== {result.job} [{head}]  data: {result.refreshed}"
                  + (f"  artifacts: {result.artifacts}" if result.artifacts else ""))
            print(_indent(result.message))
        for delivery in deliveries:
            mark = "sent" if delivery.ok else "FAILED"
            if not delivery.ok:
                failures += 1
            if not args.quiet or not delivery.ok:
                print(f"  -> {delivery.chat_id}: {mark} ({delivery.detail})")
        if not chat_ids and not args.quiet:
            print("  -> no recipients configured")

    if notifier.dry_run and not args.quiet:
        print("\n(dry run — nothing was sent. Use --send to deliver.)")
    return 1 if failures else 0


def _indent(text: str) -> str:
    """Show the message as it will read, minus the HTML tags."""
    plain = (
        text.replace("<b>", "").replace("</b>", "")
        .replace("<i>", "").replace("</i>", "")
        .replace("<code>", "").replace("</code>", "")
        .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    )
    return "\n".join("    " + line for line in plain.splitlines())


if __name__ == "__main__":
    raise SystemExit(main())
