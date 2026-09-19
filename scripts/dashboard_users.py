"""Create dashboard accounts (R13), one per team member, each with its own new password.

    python -m scripts.dashboard_users --passwords accounts.txt stanley brendah ede romeo lamine

Writes each member's password to the --passwords file (hand them out privately, then delete it)
and prints the value of DASHBOARD_USERS, which keeps only salted scrypt hashes. To add a member
later, append the printed entry to the existing value with a comma.
"""

import argparse
import secrets
from pathlib import Path

from app.web.auth import hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--passwords", type=Path, required=True, help="file to write the new passwords to")
    parser.add_argument("names", nargs="+", help="account names, e.g. first names")
    args = parser.parse_args()
    names = [name.strip().lower() for name in args.names]
    if any(not name or ":" in name or "," in name for name in names) or len(set(names)) != len(names):
        parser.error("names must be distinct, without ':' or ','")
    passwords = {name: secrets.token_urlsafe(12) for name in names}
    args.passwords.parent.mkdir(parents=True, exist_ok=True)
    args.passwords.write_text("".join(f"{name}: {password}\n" for name, password in passwords.items()), encoding="utf-8")
    print(",".join(f"{name}:{hash_password(password)}" for name, password in passwords.items()))


if __name__ == "__main__":
    main()
