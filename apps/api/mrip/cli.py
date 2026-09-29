"""``mrip-admin`` — the operator's command line.

This exists for one reason the HTTP API cannot cover: **bootstrapping**. A fresh
deployment has no accounts, so there is nobody who can call
``POST /api/auth/users``. The alternative — shipping a default administrator with
a known password — is the single most common way an on-prem system is
compromised, and :meth:`~mrip.config.Settings.check_production_ready` already
refuses other instances of that pattern.

So the first account is created here, on the host, by somebody with shell access
and the database password. Everything after that can be done over the API; the
rest of these commands exist because an administrator locked out of the UI needs
a way back in that does not involve editing tables by hand.

Passwords are read from a prompt, never from an argument: a password in
``argv`` is in the shell history, in ``ps`` output, and visible to every other
process on the host. ``--password-stdin`` exists for automated provisioning —
stdin is not argv, so a deployment script can pipe a secret in from a vault
without it ever appearing in a process listing.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path

from mrip import log
from mrip.auth import passwords
from mrip.auth.scope import SCOPE_ALL
from mrip.config import get_settings
from mrip.db import store_session
from mrip.db.schema_version import check_schema_version
from mrip.schemas import Role

__all__ = ["main"]

logger = log.get_logger("mrip.admin")


def _read_password(prompt: str, *, username: str, from_stdin: bool = False) -> str:
    """Read a password, check policy, and never echo.

    Policy is checked here rather than only at the database, so a mistyped
    12-character requirement is reported before the transaction opens.

    With ``from_stdin`` the password is read once from the pipe and a failing
    policy is fatal rather than a re-prompt: there is nobody at the keyboard to
    try again, and looping would hang a provisioning script.
    """
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise ValueError(
                "No password on stdin. Pipe one in, or drop --password-stdin."
            )
        passwords.check_password_policy(password, username=username)
        return password

    while True:
        first = getpass.getpass(f"{prompt}: ")
        second = getpass.getpass("Repeat: ")
        if first != second:
            print("Those did not match. Try again.", file=sys.stderr)
            continue
        try:
            passwords.check_password_policy(first, username=username)
        except passwords.WeakPasswordError as weak:
            for reason in weak.reasons:
                print(f"  - {reason}", file=sys.stderr)
            continue
        return first


def _parse_entities(raw: str | None) -> list[str]:
    """``--entities secl,mcl`` or ``--entities '*'`` for HQ-wide."""
    if not raw:
        return []
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


# ------------------------------------------------------------------- commands


def cmd_create_user(args: argparse.Namespace) -> int:
    entities = _parse_entities(args.entities)
    password = _read_password(
        f"Password for {args.username}",
        username=args.username,
        from_stdin=args.password_stdin,
    )

    with store_session() as store:
        if store.users.by_username(args.username) is not None:
            print(f"An account named {args.username!r} already exists.", file=sys.stderr)
            return 1

        record = store.users.create(
            args.username,
            role=Role(args.role),
            password_hash=passwords.hash_password(password),
            display_name=args.display_name,
            email=args.email,
            # A password an administrator typed is not the holder's credential
            # yet. `--no-force-change` exists for the bootstrap admin, who is the
            # same person as the holder.
            must_change_password=not args.no_force_change,
        )
        granted = store.users.replace_scopes(record.user_id, entities)
        store.audit.record(
            "user.created",
            actor_username="mrip-admin (cli)",
            subject_type="user",
            subject_id=record.user_id,
            detail={
                "username": record.username,
                "role": record.role.value,
                "entities": entities,
                "via": "cli",
            },
        )

    scope_note = (
        "every entity (HQ-wide)"
        if SCOPE_ALL in entities
        else (", ".join(entities) or "no entities yet — grant some with 'grant'")
    )
    print(f"Created {record.username} ({record.role.value}); scope: {scope_note}.")
    if granted == 0 and not entities:
        print(
            "Note: with no entity grants this account can sign in and will see an "
            "empty corpus. That is deliberate — scope is granted, never assumed."
        )
    return 0


def cmd_seed_demo(args: argparse.Namespace) -> int:
    """Build the demonstration corpus and the accounts to explore it with."""
    from mrip.demo import DEMO_ACCOUNTS, DEMO_PASSWORD, seed_demo

    print("Seeding the demonstration corpus. Each document goes through the real")
    print("pipeline — intake, digitize, extract, normalize, validate, index — so")
    print("this takes a few seconds per file.")
    print()

    outcome = seed_demo(with_documents=not args.accounts_only)

    if outcome["accounts"]:
        print(f"Created {len(outcome['accounts'])} account(s). Password for all of them:")
        print(f"    {DEMO_PASSWORD}")
        print()
        for username, role, entities, description in DEMO_ACCOUNTS:
            scope = "all entities" if "*" in entities else ", ".join(entities)
            print(f"    {username:<18} {role.value:<9} {scope:<14} {description}")
        print()
    else:
        print("Accounts already exist; left untouched.")
        print()

    if outcome["documents"]:
        counts = outcome["counts"]
        print(f"Ingested {len(outcome['documents'])} document(s):")
        print(
            f"    {counts['facts']} facts from {counts['evidence_spans']} evidence spans"
        )
        print(f"    {counts['entities']} entities, {counts['metrics']} metrics")
        print(f"    {outcome['open_conflicts']} conflict(s) awaiting adjudication")
        print()
        print("Every seeded document is flagged synthetic and shows a badge in the UI:")
        print("a demonstration figure must never be mistaken for a government source.")
    else:
        print("The demonstration documents are already in the corpus.")
    return 0


def cmd_list_users(args: argparse.Namespace) -> int:
    with store_session() as store:
        records = store.users.list_accounts()
        rows = [
            (
                record.username,
                record.role.value,
                "active" if record.is_active else "disabled",
                ",".join(
                    grant.entity_id for grant in store.users.grants_for(record.user_id)
                )
                or "-",
                record.last_login_at.strftime("%Y-%m-%d %H:%M")
                if record.last_login_at
                else "never",
                "LOCKED" if record.locked_until else "",
            )
            for record in records
        ]

    if not rows:
        print("No accounts yet. Create the first one with: mrip-admin create-user …")
        return 0

    headers = ("username", "role", "status", "entities", "last login", "")
    widths = [
        max(len(str(row[index])) for row in (*rows, headers))
        for index in range(len(headers))
    ]
    print("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip())
    for row in rows:
        print(
            "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return 0


def cmd_grant(args: argparse.Namespace) -> int:
    entities = _parse_entities(args.entities)
    if not entities:
        print(
            "Nothing to grant. Pass --entities secl,mcl or --entities '*'.",
            file=sys.stderr,
        )
        return 1

    with store_session() as store:
        user = store.users.by_username(args.username)
        if user is None:
            print(f"No account named {args.username!r}.", file=sys.stderr)
            return 1
        if args.replace:
            count = store.users.replace_scopes(user.user_id, entities)
            action = "user.scope_replaced"
        else:
            count = sum(
                store.users.grant_scope(user.user_id, entity) for entity in entities
            )
            action = "user.scope_granted"
        store.audit.record(
            action,
            actor_username="mrip-admin (cli)",
            subject_type="user",
            subject_id=user.user_id,
            detail={"entities": entities, "replace": args.replace, "via": "cli"},
        )
    print(f"{args.username}: {count} grant(s) applied ({', '.join(entities)}).")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    entities = _parse_entities(args.entities)
    with store_session() as store:
        user = store.users.by_username(args.username)
        if user is None:
            print(f"No account named {args.username!r}.", file=sys.stderr)
            return 1
        removed = sum(store.users.revoke_scope(user.user_id, e) for e in entities)
        store.audit.record(
            "user.scope_revoked",
            actor_username="mrip-admin (cli)",
            subject_type="user",
            subject_id=user.user_id,
            detail={"entities": entities, "removed": removed, "via": "cli"},
        )
    print(f"{args.username}: {removed} grant(s) revoked.")
    return 0


def cmd_set_role(args: argparse.Namespace) -> int:
    with store_session() as store:
        user = store.users.by_username(args.username)
        if user is None:
            print(f"No account named {args.username!r}.", file=sys.stderr)
            return 1
        role = Role(args.role)
        if (
            user.role is Role.ADMIN
            and role is not Role.ADMIN
            and store.users.count_active_admins() == 1
        ):
            print(
                f"{user.username} is the only active administrator. "
                "Promote another account first.",
                file=sys.stderr,
            )
            return 1
        store.users.set_role(user.user_id, role)
        store.audit.record(
            "user.updated",
            actor_username="mrip-admin (cli)",
            subject_type="user",
            subject_id=user.user_id,
            detail={"changed": {"role": role.value}, "via": "cli"},
        )
    print(f"{args.username} is now a {args.role}.")
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    with store_session() as store:
        user = store.users.by_username(args.username)
        if user is None:
            print(f"No account named {args.username!r}.", file=sys.stderr)
            return 1
        password = _read_password(
            "New password", username=user.username, from_stdin=args.password_stdin
        )
        store.users.set_password(
            user.user_id,
            passwords.hash_password(password),
            # The administrator running this knows the password, so it buys
            # exactly one login.
            must_change=True,
        )
        store.audit.record(
            "user.password_reset",
            actor_username="mrip-admin (cli)",
            subject_type="user",
            subject_id=user.user_id,
            detail={"via": "cli", "sessions_invalidated": True},
        )
    print(
        f"{args.username}: password reset. Existing sessions are signed out and a "
        "change is required at next login."
    )
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    with store_session() as store:
        user = store.users.by_username(args.username)
        if user is None:
            print(f"No account named {args.username!r}.", file=sys.stderr)
            return 1
        store.users.unlock(user.user_id)
        store.audit.record(
            "user.unlocked",
            actor_username="mrip-admin (cli)",
            subject_type="user",
            subject_id=user.user_id,
            detail={"via": "cli"},
        )
    print(f"{args.username}: unlocked.")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    with store_session() as store:
        user = store.users.by_username(args.username)
        if user is None:
            print(f"No account named {args.username!r}.", file=sys.stderr)
            return 1
        if (
            user.role is Role.ADMIN
            and user.is_active
            and store.users.count_active_admins() == 1
        ):
            print(
                f"{user.username} is the only active administrator; "
                "disabling it would lock everyone out.",
                file=sys.stderr,
            )
            return 1
        store.users.set_active(user.user_id, active=False)
        store.audit.record(
            "user.disabled",
            actor_username="mrip-admin (cli)",
            subject_type="user",
            subject_id=user.user_id,
            detail={"via": "cli", "sessions_invalidated": True},
        )
    print(f"{args.username}: disabled; sessions ended.")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    with store_session() as store:
        user = store.users.by_username(args.username)
        if user is None:
            print(f"No account named {args.username!r}.", file=sys.stderr)
            return 1
        store.users.set_active(user.user_id, active=True)
        store.audit.record(
            "user.enabled",
            actor_username="mrip-admin (cli)",
            subject_type="user",
            subject_id=user.user_id,
            detail={"via": "cli"},
        )
    print(f"{args.username}: enabled.")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    with store_session() as store:
        entries = store.audit.recent(limit=args.limit, action=args.action)
    for entry in reversed(entries):
        when = entry.occurred_at.strftime("%Y-%m-%d %H:%M:%S")
        subject = f" {entry.subject_type}:{entry.subject_id}" if entry.subject_id else ""
        print(f"{when}  {entry.actor_username or '-':<24} {entry.action}{subject}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """What this deployment is, before anyone asks it to do anything."""
    settings = get_settings()
    version = check_schema_version()
    with store_session() as store:
        counts = store.documents.count_by_state()
        accounts = len(store.users.list_accounts())
        admins = store.users.count_active_admins()

    print(f"profile          {settings.profile.value}")
    print(f"database         {settings.database_url.hosts()[0]['host']}")
    print(f"schema           {version.describe()}")
    print(f"blobs            {settings.blob_dir}")
    print(f"accounts         {accounts} ({admins} active admin)")
    print(f"documents        {sum(counts.values())} {counts or ''}")
    if accounts == 0:
        print(
            "\nNo accounts exist. Create the first administrator:\n"
            "  mrip-admin create-user <name> --role admin --entities '*' "
            "--no-force-change"
        )
    return 0


# ------------------------------------------------------------- real documents


def _human(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:,.1f} GB"


def cmd_fetch_corpus(args: argparse.Namespace) -> int:
    """Download the real CIL/CMPDI/Ministry corpus and write its manifest.

    This is the command that turns "the pipeline works on tables we wrote" into
    "the pipeline works on documents Coal India published". The documents land in
    ``data/corpus/<company>/`` and are gitignored; the manifest beside them records
    every source URL and SHA-256 and *is* committed, so the same corpus can be
    re-fetched and verified byte for byte.
    """
    from mrip import corpus

    root = args.root.resolve()
    manifest_path = args.manifest.resolve()
    companies = tuple(c.strip().lower() for c in args.companies.split(",") if c.strip())

    if args.verify_only:
        result = corpus.verify(root, manifest_path)
        total = sum(len(v) for v in result.values())
        if total == 0:
            print(f"No manifest at {manifest_path}. Run without --verify-only first.")
            return 1
        print(f"ok       {len(result['ok'])}")
        print(f"missing  {len(result['missing'])}")
        print(f"changed  {len(result['changed'])}")
        for label in ("missing", "changed"):
            for path in result[label][:20]:
                print(f"  {label:8s} {path}")
        # A changed file is a real failure: ground truth pinned to a document is
        # worthless if the document was replaced. A missing one is just not fetched.
        return 1 if result["changed"] else 0

    seeds = corpus.SEEDS
    if companies:
        seeds = tuple(seed for seed in seeds if seed.company in companies)
        if not seeds:
            known = sorted({seed.company for seed in corpus.SEEDS})
            print(f"error: no seeds for {companies}; known: {', '.join(known)}")
            return 2

    print(f"Crawling {len(seeds)} seed pages at {corpus.POLITE_DELAY}s per host.")
    print("Public documents only; robots.txt is read and obeyed.\n")

    report = corpus.fetch_all(
        root,
        companies=companies or None,
        limit_per_seed=args.limit,
        dry_run=args.dry_run,
        seeds=seeds,
    )

    summary = corpus.summarise(report.entries)
    print(f"\n{'company':16s} {'docs':>6s} {'size':>12s}  types")
    print("-" * 64)
    for company, row in sorted(summary.items()):
        types = " ".join(f"{k}:{v}" for k, v in sorted(row["types"].items()))
        print(f"{company:16s} {row['documents']:6d} {_human(row['bytes']):>12s}  {types}")
    print("-" * 64)
    print(
        f"{'total':16s} {len(report.entries):6d} "
        f"{_human(sum(e.size_bytes for e in report.entries)):>12s}"
    )
    print(
        f"\ndownloaded {report.downloaded} · already present {report.already_present} "
        f"· failed {len(report.failed)} · skipped {len(report.skipped)}"
    )
    for url, reason in report.failed[:10]:
        print(f"  failed   {reason:20s} {url[:90]}")
    for url, reason in report.skipped[:10]:
        print(f"  skipped  {reason:20s} {url[:90]}")

    if args.dry_run:
        print("\nDry run: nothing was downloaded and no manifest was written.")
        return 0

    corpus.write_manifest(report.entries, manifest_path)
    print(f"\nManifest: {manifest_path}")
    print(f"Documents: {root}  (gitignored — the manifest is what is committed)")
    print("\nIngest them through the real pipeline with:")
    print("  mrip-admin ingest-corpus")
    return 0


def cmd_ingest_corpus(args: argparse.Namespace) -> int:
    """Run the fetched documents through the pipeline and report the result.

    The point of this command is its output. Any pipeline can be made to look
    good on a corpus by loosening the boundary until nothing is refused; the
    useful thing is the breakdown — how many documents produced facts, and for
    the ones that did not, which stage declined them and why.
    """
    from mrip import corpus_ingest

    root = args.root.resolve()
    companies = tuple(c.strip().lower() for c in args.companies.split(",") if c.strip())
    only = [name.strip() for name in args.only.split(",") if name.strip()]

    report = corpus_ingest.ingest_corpus(
        root,
        limit=args.limit,
        companies=companies or None,
        only=only or None,
        skip_ready=not args.include_ready,
        manifest=args.manifest.resolve() if args.manifest else None,
    )
    print()
    print(corpus_ingest.format_summary(report.summary()))
    if report.already_ready:
        print(f"\n({report.already_ready} documents were already ready and skipped.)")
    # A run that produced no facts at all is a failure worth a non-zero exit: a
    # cron job watching this should notice.
    return 0 if any(o.facts for o in report.outcomes) else 1


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mrip-admin",
        description="Operator commands for an MRIP deployment.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="deployment summary")
    status.set_defaults(handler=cmd_status)

    create = subparsers.add_parser("create-user", help="create an account")
    create.add_argument("username")
    create.add_argument(
        "--role", choices=[role.value for role in Role], default=Role.VIEWER.value
    )
    create.add_argument(
        "--entities",
        help="comma-separated entity ids this account may read, or '*' for HQ-wide",
    )
    create.add_argument("--display-name")
    create.add_argument("--email")
    create.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting (for provisioning)",
    )
    create.add_argument(
        "--no-force-change",
        action="store_true",
        help="do not require a password change at first login (bootstrap admin only)",
    )
    create.set_defaults(handler=cmd_create_user)

    seed = subparsers.add_parser(
        "seed-demo",
        help="create demonstration accounts and a small synthetic corpus",
    )
    seed.add_argument(
        "--accounts-only",
        action="store_true",
        help="create the accounts but ingest no documents",
    )
    seed.set_defaults(handler=cmd_seed_demo)

    listing = subparsers.add_parser("list-users", help="list accounts")
    listing.set_defaults(handler=cmd_list_users)

    grant = subparsers.add_parser("grant", help="grant entity access")
    grant.add_argument("username")
    grant.add_argument("--entities", required=True)
    grant.add_argument(
        "--replace",
        action="store_true",
        help="replace the account's grants instead of adding to them",
    )
    grant.set_defaults(handler=cmd_grant)

    revoke = subparsers.add_parser("revoke", help="revoke entity access")
    revoke.add_argument("username")
    revoke.add_argument("--entities", required=True)
    revoke.set_defaults(handler=cmd_revoke)

    role = subparsers.add_parser("set-role", help="change an account's role")
    role.add_argument("username")
    role.add_argument("role", choices=[value.value for value in Role])
    role.set_defaults(handler=cmd_set_role)

    reset = subparsers.add_parser("reset-password", help="set a new password")
    reset.add_argument("username")
    reset.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting",
    )
    reset.set_defaults(handler=cmd_reset_password)

    unlock = subparsers.add_parser("unlock", help="clear a failed-login lockout")
    unlock.add_argument("username")
    unlock.set_defaults(handler=cmd_unlock)

    disable = subparsers.add_parser("disable", help="disable an account")
    disable.add_argument("username")
    disable.set_defaults(handler=cmd_disable)

    enable = subparsers.add_parser("enable", help="re-enable an account")
    enable.add_argument("username")
    enable.set_defaults(handler=cmd_enable)

    audit = subparsers.add_parser("audit", help="print recent audit entries")
    audit.add_argument("--limit", type=int, default=50)
    audit.add_argument("--action", help="filter to one action, e.g. auth.login_failed")
    audit.set_defaults(handler=cmd_audit)

    fetch = subparsers.add_parser(
        "fetch-corpus",
        help="download the real CIL/CMPDI/Ministry document corpus",
        description=(
            "Crawls the public websites of Coal India, its subsidiaries, CMPDI, the "
            "Coal Controller Organisation and the Ministry of Coal, and files every "
            "report, statement and statistics volume it finds under "
            "data/corpus/<company>/. One request per second per host; robots.txt is "
            "read and obeyed. The documents are gitignored; the manifest recording "
            "each source URL and SHA-256 is committed."
        ),
    )
    fetch.add_argument(
        "--root",
        type=Path,
        default=Path("data/corpus"),
        help="where to write the documents (default: data/corpus)",
    )
    fetch.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/corpus/manifest.json"),
        help="where to write the committed manifest",
    )
    fetch.add_argument(
        "--companies",
        default="",
        help="comma-separated company ids to limit the crawl, e.g. cil,mcl,moc",
    )
    fetch.add_argument(
        "--limit",
        type=int,
        default=None,
        help="at most this many documents per seed page (for a quick trial run)",
    )
    fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="crawl and report what would be fetched, downloading nothing",
    )
    fetch.add_argument(
        "--verify-only",
        action="store_true",
        help="re-hash the local corpus against the manifest and report drift",
    )
    fetch.set_defaults(handler=cmd_fetch_corpus)

    ingest = subparsers.add_parser(
        "ingest-corpus",
        help="push the fetched real documents through the pipeline",
        description=(
            "Registers the documents under data/corpus and runs all six ingestion "
            "stages over them, then prints what the pipeline actually produced: "
            "how many pages, how many facts, and — for the documents that yielded "
            "no figure — why. Run `mrip-admin fetch-corpus` first. This is the "
            "command that measures extraction against real government filings "
            "rather than against tables this project wrote."
        ),
    )
    ingest.add_argument(
        "--root",
        type=Path,
        default=Path("data/corpus"),
        help="where the documents are (default: data/corpus)",
    )
    ingest.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="manifest to read source URLs from (default: <root>/manifest.json)",
    )
    ingest.add_argument(
        "--limit",
        type=int,
        default=None,
        help="ingest at most this many documents, smallest first",
    )
    ingest.add_argument(
        "--companies",
        default="",
        help="comma-separated company ids to limit to, e.g. cil,mcl",
    )
    ingest.add_argument(
        "--only",
        default="",
        help="comma-separated filenames to ingest, for working on one document",
    )
    ingest.add_argument(
        "--include-ready",
        action="store_true",
        help="re-run documents already marked ready",
    )
    ingest.set_defaults(handler=cmd_ingest_corpus)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    log.configure_logging(settings)
    args = build_parser().parse_args(argv)
    handler = args.handler
    try:
        result: int = handler(args)
        return result
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as failure:
        # No traceback by default: the usual cause is an unreachable database or
        # a duplicate username, and a wall of stack frames buries which.
        print(f"error: {failure}", file=sys.stderr)
        logger.debug("mrip-admin failed", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
