"""Explicit account-default adoption, preview only unless --apply is supplied."""

import argparse
import contextlib
import fcntl
import json
import os
import sqlite3
import uuid
from pathlib import Path

from manage_headscale import validate_config
from scripts.provision_subscriber import canonical_account_id, write_config_atomically
from scripts.provisioning_worker import IMAGE, IMAGE_DIGEST, canonical, private_json


def adopt(args):
    account = canonical_account_id(args.account_id)
    tailnet = canonical_account_id(args.tailnet_id)
    ledger = Path(args.ledger)
    settings = private_json(args.worker_settings)
    config_path = Path(settings["config"])
    # Do not race the supervised worker or an operator config writer.
    with ledger.with_suffix(".worker.lock").open("a+") as worker_lock:
        os.chmod(worker_lock.name, 0o600)
        fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with config_path.with_name(f".{config_path.name}.lock").open("a+") as config_lock:
            fcntl.flock(config_lock, fcntl.LOCK_EX)
            config = private_json(config_path)
            validate_config(config)
            matches = [i for i in config["headscale"]["instances"] if i["id"] == args.instance_id]
            if len(matches) != 1:
                raise ValueError("exactly one configured instance is required")
            spec = matches[0]
            if (spec.get("subscriber_account_id") != account
                    or spec.get("management_mode") != "managed"
                    or spec.get("organization_mode") != "isolated"):
                raise ValueError("only the exact owned isolated managed generation may be adopted")
            if spec.get("image", IMAGE) != IMAGE:
                raise ValueError("pinned Headscale version migration must be performed separately")
            if (spec.get("provisioning_tailnet_id") not in (None, tailnet)
                    or not isinstance(spec.get("subscriber_generation"), int)
                    or isinstance(spec["subscriber_generation"], bool)
                    or spec["subscriber_generation"] < 1):
                raise ValueError("invalid or already bound generation")
            result = {
                "action": "adopt", "applied": bool(args.apply),
                "tailnet_id": tailnet, "generation": spec["subscriber_generation"],
                "server_url": spec["server_url"],
                "readiness": "not_verified",
            }
            with contextlib.closing(sqlite3.connect(ledger, timeout=2)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT version FROM provisioning_version").fetchall() != [(1,)]:
                    raise ValueError("unsupported ledger version")
                existing = db.execute("SELECT tailnet,spec FROM account_defaults WHERE account=?",
                                      (account,)).fetchone()
                if existing and existing[0] != tailnet:
                    raise ValueError("account default cannot be replaced or moved by adoption")
                if existing and existing[1] and json.loads(existing[1])["id"] != args.instance_id:
                    raise ValueError("default is already bound to another instance")
                if not args.apply:
                    return result
                spec["provisioning_tailnet_id"] = tailnet
                spec["image"] = IMAGE
                spec["image_digest"] = IMAGE_DIGEST
                if existing and existing[1]:
                    if json.loads(existing[1]) != spec:
                        raise ValueError("adopted instance configuration has drifted")
                    # Repair only the projection; retain operation identity and readiness.
                    write_config_atomically(config_path, config)
                    return result
                document = {
                    "api_version": "1", "account_id": account, "tailnet_id": tailnet,
                    "generation": spec["subscriber_generation"], "operation_id": str(uuid.uuid4()),
                    "state": "blocked", "phase": "allocation", "server_url": spec["server_url"],
                    "readiness": {"checked_at": None, "valid_until": None},
                    "error": {"code": "adoption_readiness_required", "retryable": True},
                }
                db.execute(
                    "INSERT INTO account_defaults(account,tailnet,document,spec,due) VALUES(?,?,?,?,1) "
                    "ON CONFLICT(account) DO UPDATE SET document=excluded.document,spec=excluded.spec,due=1",
                    (account, tailnet, canonical(document), canonical(spec)),
                )
            # Ledger intent precedes projection. Retry the identical adoption on unknown outcome.
            write_config_atomically(config_path, config)
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--worker-settings", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--tailnet-id", required=True)
    parser.add_argument("--instance-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(adopt(args)))
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        parser.exit(1, f"adoption failed: {type(exc).__name__}; no deployment performed\n")


if __name__ == "__main__":
    main()
