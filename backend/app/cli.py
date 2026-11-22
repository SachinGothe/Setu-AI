"""Administrative maintenance commands. Credentials are read only from backend/.env."""

import argparse
import sys

from app.core.config import get_settings
from app.services.admin_bootstrap import bootstrap_admin


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=["bootstrap-admin", "import-reference-pdfs"])
    parser.add_argument("--pm-vishwakarma")
    parser.add_argument("--pm-kisan")
    parser.add_argument("--scholarship")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.admin_email:
        print("Set ADMIN_EMAIL in backend/.env, then rerun this command.", file=sys.stderr)
        return 2
    try:
        if args.command == "bootstrap-admin":
            if not settings.admin_password:
                print("Set ADMIN_PASSWORD in backend/.env, then rerun this command.", file=sys.stderr)
                return 2
            result = bootstrap_admin(settings.admin_email, settings.admin_password)
            print(f"Admin account {result}. Password was hashed; existing account data was preserved.")
            return 0
        if not all((args.pm_vishwakarma, args.pm_kisan, args.scholarship)):
            print("Provide --pm-vishwakarma, --pm-kisan, and --scholarship file paths.", file=sys.stderr)
            return 2
        from app.services.document_service import import_curated_reference_pdfs
        records = import_curated_reference_pdfs(settings.admin_email, {
            "pm_vishwakarma": args.pm_vishwakarma,
            "pm_kisan": args.pm_kisan,
            "scholarship": args.scholarship,
        })
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for item in records:
        print(f"Imported {item['title']} ({item['processing_status']}, {item['size_bytes']} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
