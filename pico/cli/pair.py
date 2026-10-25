"""CLI command to generate a local operator pairing code."""

import argparse
import sys
from pico.services.operator_session import OperatorSessionService


def main() -> None:
    parser = argparse.ArgumentParser(description="PICO Local Operator Pairing")
    parser.add_argument(
        "--approver",
        default="human_operator",
        help="Operator identity name (default: human_operator)",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=300,
        help="Pairing code validity in seconds (default: 300)",
    )
    args = parser.parse_args()

    service = OperatorSessionService.get_instance()
    pairing = service.generate_pairing_code(approver=args.approver, ttl_seconds=args.ttl)

    print("=" * 50)
    print(" PICO Local Operator Pairing Code")
    print("=" * 50)
    print(f" Pairing Code : {pairing.code}")
    print(f" Approver     : {pairing.approver}")
    print(f" Expires At   : {pairing.expires_at.isoformat()} UTC")
    print(f" Valid for    : {args.ttl} seconds")
    print("=" * 50)
    print("Enter this 6-digit code in the PICO web interface to pair your session.")
    print("=" * 50)


if __name__ == "__main__":
    main()
