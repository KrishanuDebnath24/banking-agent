"""Copy the app's passwords and API keys from .env to AWS Secrets Manager, where the containers read them.

    python -m infra.app_secrets push      create or update the secret "banking-agent/app"
    python -m infra.app_secrets check     which keys are filled in (never shows a value)
    python -m infra.app_secrets delete    delete it (AWS keeps it 7 days in case you change your mind)

Run from the project folder. Values go from your .env straight to AWS over HTTPS: they're never
printed, logged, or put in a template. Uses the AWS login from  aws configure  (or aws sso login).
A secret costs $0.40 a month, charged by the hour: delete it if you stop doing demos.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys

from infra.settings import PROJECT, SECRET_KEYS, SECRET_NAME, read_env


def needed(env: dict[str, str]) -> set[str]:
    """Keys the app can't work without, given the rest of .env."""
    keys = {"OBP_CONSUMER_KEY", "OBP_CONSUMER_SECRET", "APP_SECRET"}
    if env.get("OBP_FAKE") != "1":
        keys |= {"CUSTOMER_A_PASSWORD", "CUSTOMER_B_PASSWORD"}
    if env.get("MODEL", "").startswith("anthropic:"):
        keys.add("ANTHROPIC_API_KEY")
    if env.get("EMAIL_BACKEND", "console").strip().lower() == "smtp":
        keys.add("SMTP_PASSWORD")
    if env.get("LANGSMITH_TRACING", "").lower() == "true":
        keys.add("LANGSMITH_API_KEY")
    if env.get("GOOGLE_CLIENT_ID", "").strip():
        keys.add("GOOGLE_CLIENT_SECRET")
    return keys


def client(region: str):
    import boto3
    return boto3.client("secretsmanager", region_name=region)


def report(values: dict[str, str], env: dict[str, str]) -> int:
    missing = sorted(k for k in needed(env) if not values.get(k))
    for key in SECRET_KEYS:
        print(f"  {key:<22} {'set' if values.get(key) else 'empty'}")
    if missing:
        print(f"\nWarning: the app needs these but they're empty in .env: {', '.join(missing)}")
        if "APP_SECRET" in missing:
            print('Make an APP_SECRET with:  python -c "import secrets; print(secrets.token_urlsafe(32))"'
                  "  and paste it into .env.")
        return 1
    return 0


def _stored(sm) -> dict[str, str]:
    """What the secret holds now ({} if it doesn't exist or is deleted)."""
    try:
        return json.loads(sm.get_secret_value(SecretId=SECRET_NAME)["SecretString"])
    except (sm.exceptions.ResourceNotFoundException, sm.exceptions.InvalidRequestException):
        return {}


def push(region: str, env: dict[str, str]) -> int:
    sm = client(region)
    values = {key: env.get(key, "") for key in SECRET_KEYS}  # every key present: containers expect all of them
    if not values.get("ORIGIN_SECRET"):
        # CloudFront's secret header: nobody needs to type it, so it's made here once and then kept
        # (CloudFront picks it up when you deploy).
        values["ORIGIN_SECRET"] = _stored(sm).get("ORIGIN_SECRET") or secrets.token_urlsafe(32)
    body = json.dumps(values)
    try:
        info = sm.describe_secret(SecretId=SECRET_NAME)
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(Name=SECRET_NAME, SecretString=body,
                         Description=f"{PROJECT}: app passwords and API keys, copied from .env",
                         Tags=[{"Key": "project", "Value": PROJECT}])
        action = "Created"
    else:
        action = "Updated"
        if info.get("DeletedDate"):  # deleted less than 7 days ago: bring it back first
            sm.restore_secret(SecretId=SECRET_NAME)
            action = "Restored and updated"
        sm.put_secret_value(SecretId=SECRET_NAME, SecretString=body)
    print(f"{action} {SECRET_NAME} in {region}:")
    return report(values, env)


def check(region: str, env: dict[str, str]) -> int:
    sm = client(region)
    try:
        values = json.loads(sm.get_secret_value(SecretId=SECRET_NAME)["SecretString"])
    except sm.exceptions.ResourceNotFoundException:
        print(f"{SECRET_NAME} doesn't exist in {region} yet. Run:  python -m infra.app_secrets push")
        return 1
    print(f"{SECRET_NAME} in {region}:")
    absent = [k for k in SECRET_KEYS if k not in values]
    if absent:
        print(f"  missing keys (the containers won't start): {', '.join(absent)}. Run push again.")
    return report(values, env) or (1 if absent else 0)


def delete(region: str, now: bool) -> int:
    sm = client(region)
    try:
        if now:
            sm.delete_secret(SecretId=SECRET_NAME, ForceDeleteWithoutRecovery=True)
            print(f"Deleted {SECRET_NAME} for good.")
        else:
            sm.delete_secret(SecretId=SECRET_NAME, RecoveryWindowInDays=7)
            print(f"Deleted {SECRET_NAME}; 'push' within 7 days brings it back.")
    except sm.exceptions.ResourceNotFoundException:
        print(f"{SECRET_NAME} doesn't exist in {region}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["push", "check", "delete"])
    ap.add_argument("--now", action="store_true", help="delete: skip the 7-day recovery window")
    args = ap.parse_args()
    env = read_env()
    region = env.get("AWS_REGION", "").strip() or "eu-west-1"
    if args.command == "push":
        return push(region, env)
    if args.command == "check":
        return check(region, env)
    return delete(region, args.now)


if __name__ == "__main__":
    sys.exit(main())
