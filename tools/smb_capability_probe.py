from __future__ import annotations

import argparse
import getpass
import json
from collections.abc import Sequence
from dataclasses import asdict

from nasmove.core.model import ConnectionConfig, ConnectionProfileId, RemotePath
from nasmove.smb.capability_probe import CapabilityReport, SmbCapabilityProbe
from nasmove.smb.error_mapping import redacted_error_code
from nasmove.smb.smbprotocol_gateway import SmbProtocolGateway


def run_probe(
    *,
    config: ConnectionConfig,
    password: str,
    target: RemotePath,
) -> CapabilityReport:
    gateway = SmbProtocolGateway()
    try:
        gateway.connect(config, password)
        return SmbCapabilityProbe(gateway).run(target)
    except Exception as error:  # noqa: BLE001 - CLI emits only a redacted connection code
        return CapabilityReport.failed(f"connect:{redacted_error_code(error)}")
    finally:
        gateway.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe a dedicated non-production SMB directory")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--share")
    parser.add_argument("--username")
    parser.add_argument("--domain")
    parser.add_argument("--target")
    parser.add_argument("--allow-unencrypted", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    host = args.host or input("NAS address: ").strip()
    port = args.port or int(input("NAS port [445]: ").strip() or "445")
    share = args.share or input("SMB share: ").strip()
    username = args.username or input("NAS username: ").strip()
    domain_value = args.domain if args.domain is not None else input("NAS domain [optional]: ")
    domain = domain_value.strip() or None
    target_value = args.target or input("Dedicated non-production test directory: ").strip()
    password = getpass.getpass("NAS password: ")
    config = ConnectionConfig(
        profile_id=ConnectionProfileId("interactive-capability-probe"),
        display_name="Interactive SMB capability probe",
        host=host,
        port=port,
        share=share,
        username=username,
        domain=domain,
        require_encryption=not args.allow_unencrypted,
    )
    report = run_probe(config=config, password=password, target=RemotePath(target_value))
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.all_supported else 1


if __name__ == "__main__":
    raise SystemExit(main())
