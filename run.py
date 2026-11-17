#!/usr/bin/env python
"""Assay: the TrainifyHub lead validation pipeline.

Each stage is its own command so you can re-run one without redoing the rest.

    python run.py init
    python run.py intake --trade roofing --location birmingham --limit 30
    python run.py identity
    python run.py domain
    python run.py score
    python run.py route
    python run.py screen --export screening/to_screen.csv
    python run.py screen --import screening/screened.csv --receipt "tpsmydata 2026-11-16 ref 123"
    python run.py serve
    python run.py export --out exports/pilot.xlsx
    python run.py status
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal, init_db
from app.models import CallGate, Company, EmailGate, Evidence, GATE_APPROVED


def cmd_init(_: argparse.Namespace) -> int:
    init_db()
    print("Database ready.")
    return 0


def cmd_import_workbook(args: argparse.Namespace) -> int:
    from app.stages import s00_import_workbook

    with SessionLocal() as session:
        result = s00_import_workbook.run(
            session, workbook_path=args.path, cohort=args.cohort
        )
        session.commit()
    print(
        f"Imported {result['imported']} companies "
        f"({result['skipped']} already present) and {result['evidence']} evidence rows."
    )
    print("No gate state or screening result was carried over. Both routes start at HOLD.")
    return 0


def cmd_intake(args: argparse.Namespace) -> int:
    from app.stages import s01_intake

    with SessionLocal() as session:
        result = s01_intake.run(
            session,
            trade=args.trade,
            location=args.location,
            limit=args.limit,
            cohort=args.cohort,
        )
        session.commit()
    print(
        f"Stage 01 intake: found {result['found']}, "
        f"new {result['new']}, refreshed {result['refreshed']}."
    )
    return 0


def cmd_identity(args: argparse.Namespace) -> int:
    from app.stages import s02_identity

    with SessionLocal() as session:
        result = s02_identity.run(session, limit=args.limit)
        session.commit()
    print(
        f"Stage 02 identity: checked {result['checked']}, "
        f"active {result['active']}, held {result['held']}."
    )
    return 0


def cmd_domain(args: argparse.Namespace) -> int:
    from app.stages import s04_domain

    with SessionLocal() as session:
        result = s04_domain.run(session, limit=args.limit)
        session.commit()
    print(
        f"Stages 03 to 05: checked {result['checked']}, "
        f"linked {result['linked']}, candidate {result['candidate']}, "
        f"nothing found {result['none']}, with a phone number {result['contacts']}."
    )
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from app.stages import s08_score

    with SessionLocal() as session:
        result = s08_score.run(session, limit=args.limit, rescore_all=args.all)
        session.commit()
    print(
        f"Stage 08 score: scored {result['scored']} "
        f"(strong {result['strong']}, review {result['review']}, "
        f"enrich {result['enrich']})."
    )
    print("Reminder: a score prioritises research. It does not authorise contact.")
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    from app.stages import s09_route

    with SessionLocal() as session:
        result = s09_route.run(session, limit=args.limit, reroute_all=args.all)
        session.commit()
    print(
        f"Stage 09 route: {result['routed']} routed. "
        f"Auto checks pass for {result['call_ready']} call records and "
        f"{result['email_ready']} email records."
    )
    print("Nothing is contactable until a named person approves it in the dashboard.")
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    from app.screening import get_provider
    from app.screening.manual import export_for_screening
    from app.stages import s11_screen

    if args.export:
        with SessionLocal() as session:
            gates = s11_screen.due_for_screening(session)
            phones = sorted({g.phone_e164 for g in gates if g.phone_e164})
        path = export_for_screening(phones, args.export)
        print(f"Wrote {len(phones)} numbers to {path}.")
        print("Upload that file to your screening provider, then import their result.")
        return 0

    if args.test:
        provider = get_provider(args.provider)
        results = provider.screen([args.test])
        for phone, result in results.items():
            print(f"{phone}: {result.summary}")
            print(f"  receipt: {result.receipt}")
        provider.close()
        return 0

    kwargs = {}
    if args.import_file:
        kwargs["result_path"] = args.import_file
    if args.receipt:
        kwargs["receipt"] = args.receipt

    provider = get_provider(args.provider, **kwargs)
    with SessionLocal() as session:
        result = s11_screen.run(session, provider=provider, limit=args.limit)
        session.commit()
    provider.close()
    print(
        f"Stage 11 screening: {result['screened']} numbers, "
        f"clear {result['clear']}, registered {result['registered']}, "
        f"errors {result['errors']}."
    )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from app.export.to_excel import export

    with SessionLocal() as session:
        path = export(session, args.out)
    print(f"Workbook written to {path}.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("app.web.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    with SessionLocal() as session:
        total = session.scalar(select(func.count(Company.id))) or 0
        evidence = session.scalar(select(func.count(Evidence.id))) or 0
        stages = session.execute(
            select(Company.stage, func.count(Company.id)).group_by(Company.stage)
        ).all()
        call_ok = session.scalar(
            select(func.count(CallGate.id)).where(
                CallGate.human_decision == GATE_APPROVED
            )
        ) or 0
        email_ok = session.scalar(
            select(func.count(EmailGate.id)).where(
                EmailGate.human_decision == GATE_APPROVED
            )
        ) or 0

    print(f"Companies: {total}")
    print(f"Evidence rows: {evidence}")
    for stage, count in sorted(stages):
        print(f"  {stage}: {count}")
    print(f"Authorised to call: {call_ok}")
    print(f"Authorised to email: {email_ok}")
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Run intake through routing in one go."""
    for step in (cmd_intake, cmd_identity, cmd_domain, cmd_score, cmd_route):
        code = step(args)
        if code:
            return code
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database tables").set_defaults(func=cmd_init)

    p = sub.add_parser(
        "import-workbook", help="load the existing 30-record pilot workbook"
    )
    p.add_argument("--path", required=True, help="path to the .xlsx")
    p.add_argument("--cohort", default="founder-pilot-30")
    p.set_defaults(func=cmd_import_workbook)

    p = sub.add_parser("intake", help="stage 01: pull companies from Companies House")
    p.add_argument("--trade", required=True, help="roofing, scaffolding, electrical, building, groundworks, plumbing")
    p.add_argument("--location", default=None, help="registered office town or city")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--cohort", default=None, help="label this batch, e.g. pilot-100")
    p.set_defaults(func=cmd_intake)

    p = sub.add_parser("identity", help="stage 02: re-check live register status")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_identity)

    p = sub.add_parser("domain", help="stages 03 to 05: website, trading identity, contacts")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_domain)

    p = sub.add_parser("score", help="stage 08: evidence confidence score")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--all", action="store_true", help="rescore every record")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("route", help="stage 09: build the two independent gates")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--all", action="store_true", help="re-evaluate every record")
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("screen", help="stage 11: TPS and CTPS screening")
    p.add_argument("--export", metavar="PATH", help="write numbers due for screening to a CSV")
    p.add_argument("--import", dest="import_file", metavar="PATH", help="read provider results from a CSV")
    p.add_argument("--receipt", help="provider, date and reference, stored as the audit receipt")
    p.add_argument("--provider", default=None, help="manual or tpsmydata")
    p.add_argument("--test", metavar="NUMBER", help="screen one number and print the raw verdict")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("export", help="write the workbook")
    p.add_argument("--out", default="exports/assay.xlsx")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("serve", help="run the review dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("status", help="counts by stage").set_defaults(func=cmd_status)

    p = sub.add_parser("pipeline", help="run intake through routing in one go")
    p.add_argument("--trade", required=True)
    p.add_argument("--location", default=None)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--cohort", default=None)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_pipeline)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except OperationalError as exc:
        # A 60-line SQLAlchemy traceback for "the database is not running" is
        # not a useful first-run experience.
        from app.config import settings

        print("Could not reach the database.", file=sys.stderr)
        print(f"  DATABASE_URL = {settings.database_url}", file=sys.stderr)
        if settings.database_url.startswith("postgresql"):
            print(
                "  Postgres does not appear to be running. Start it, or switch "
                "DATABASE_URL in .env to sqlite:///./assay.db for the pilot.",
                file=sys.stderr,
            )
        print(f"  Underlying error: {exc.orig}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        # Almost always a workbook or CSV path that does not exist. A stack
        # trace tells the person nothing they cannot see from the message.
        from pathlib import Path

        missing = Path(exc.filename or str(exc))
        print(f"File not found: {missing}", file=sys.stderr)
        if not missing.is_absolute():
            print(
                f"  That is a relative path, so it was looked for inside "
                f"{Path.cwd()}",
                file=sys.stderr,
            )
            print(
                "  Either copy the file into that folder, or pass the full "
                "path in quotes.",
                file=sys.stderr,
            )
        parent = missing.parent if missing.is_absolute() else Path.cwd()
        if parent.exists():
            nearby = sorted(p.name for p in parent.glob(f"*{missing.suffix}"))[:8]
            if nearby:
                print(
                    f"  Files with that extension in {parent}: "
                    + ", ".join(nearby),
                    file=sys.stderr,
                )
            else:
                print(f"  No {missing.suffix} files found in {parent}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        # Missing API keys and similar configuration problems raise these with
        # a message already written for a person to read.
        print(f"{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
