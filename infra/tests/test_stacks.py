"""Checks on the AWS setup, without an AWS account: builds the templates and inspects them.

    pip install -r infra/requirements.txt
    python infra/tests/test_stacks.py

Also runs the AWS security checks (cdk-nag): every finding is either fixed or accepted with a
written reason in stacks/bank.py. With moto installed (pip install "moto[secretsmanager]") it also
tests python -m infra.app_secrets against a pretend Secrets Manager.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

INFRA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INFRA))
sys.path.insert(0, str(INFRA.parent))
TMP = Path(tempfile.mkdtemp(prefix="bank-infra-test-"))

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Template  # noqa: E402

from settings import SECRET_KEYS, Settings, read_env  # noqa: E402
from stacks.bank import BankStack  # noqa: E402
from stacks.budget import BudgetStack  # noqa: E402

OK = True
ENV = cdk.Environment(account="123456789012", region="eu-west-1")
SECRET_VALUES = {key: f"{key.lower()}-NEVER-IN-A-TEMPLATE" for key in SECRET_KEYS}
PLAIN = {"MODEL": "anthropic:claude-haiku-5-5", "OBP_BASE_URL": "https://apisandbox.openbankproject.com",
         "CUSTOMER_A_USERNAME": "robert.uk.29@example.com", "EMAIL_BACKEND": "smtp"}


def check(label, cond):
    global OK
    OK &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label)


def env_file(**extra) -> Path:
    path = TMP / f"{len(list(TMP.iterdir()))}.env"
    path.write_text("\n".join(f"{k}={v}" for k, v in {**PLAIN, **SECRET_VALUES, **extra}.items()) + "\n")
    return path


def build(allowed_cidr="203.0.113.7/32", nag=False, outdir=None):
    settings = Settings.load(read_env(env_file(ALLOWED_CIDR=allowed_cidr, AWS_REGION="eu-west-1")))
    settings.account = "123456789012"
    app = cdk.App(outdir=str(outdir or TMP / "cdk.out"))
    if nag:
        from cdk_nag import AwsSolutionsChecks
        cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))
    bank = BankStack(app, "BankApp", env=ENV, settings=settings)
    budget = BudgetStack(app, "BankBudget", env=ENV, limit_usd=25, email="alerts@example.test")
    return app, bank, budget


def ingress(template: Template) -> list[dict]:
    """Every inbound rule, inline or separate, as plain dicts."""
    rules = []
    for res in template.to_json()["Resources"].values():
        if res["Type"] == "AWS::EC2::SecurityGroup":
            rules += [{"group": res["Properties"]["GroupDescription"], **r}
                      for r in res["Properties"].get("SecurityGroupIngress", [])]
        if res["Type"] == "AWS::EC2::SecurityGroupIngress":
            rules.append(res["Properties"])
    return rules


def main() -> int:
    app, bank, budget = build()
    t, b = Template.from_stack(bank), Template.from_stack(budget)
    res = t.to_json()["Resources"]

    # ---- budget ----
    spec = next(iter(b.to_json()["Resources"].values()))["Properties"]
    check("budget: $25 a month, counted before credits",
          spec["Budget"]["BudgetLimit"] == {"Amount": 25, "Unit": "USD"} and spec["Budget"]["TimeUnit"] == "MONTHLY"
          and spec["Budget"]["CostTypes"]["IncludeCredit"] is False)
    alerts = sorted((n["Notification"]["NotificationType"], n["Notification"]["Threshold"])
                    for n in spec["NotificationsWithSubscribers"])
    check("budget: emails at 50%, 80%, 100% spent and when heading past 100%",
          alerts == [("ACTUAL", 50), ("ACTUAL", 80), ("ACTUAL", 100), ("FORECASTED", 100)]
          and all(s["Address"] == "alerts@example.test" for n in spec["NotificationsWithSubscribers"]
                  for s in n["Subscribers"]))
    try:
        BudgetStack(cdk.App(), "NoEmail", env=ENV, limit_usd=25, email="")
        check("budget without an email is refused", False)
    except ValueError:
        check("budget without an email is refused", True)

    # ---- network and cost ----
    check("no NAT gateway, no Elastic IP (the costly parts)",
          not any(r["Type"] in ("AWS::EC2::NatGateway", "AWS::EC2::EIP") for r in res.values()))
    db = next(r for r in res.values() if r["Type"] == "AWS::RDS::DBInstance")["Properties"]
    check("database: smallest Postgres 17, encrypted, not public, one zone",
          db["DBInstanceClass"] == "db.t4g.micro" and db["EngineVersion"] == "17" and db["StorageEncrypted"]
          and db["PubliclyAccessible"] is False and db["MultiAZ"] is False)
    subnets = next(r for r in res.values() if r["Type"] == "AWS::RDS::DBSubnetGroup")["Properties"]["SubnetIds"]
    check("database lives in the isolated (no internet) subnets",
          all("data" in s["Ref"] for s in subnets))

    # ---- who can reach what ----
    rules = ingress(t)
    from_internet = [r for r in rules if "CidrIp" in r or "CidrIpv6" in r]
    check("only your address can reach the load balancer, only on port 80",
          from_internet == [{"group": "Load balancer: http from ALLOWED_CIDR only", "CidrIp": "203.0.113.7/32",
                             "Description": "ALLOWED_CIDR in .env", "FromPort": 80, "IpProtocol": "tcp",
                             "ToPort": 80}])
    ports = sorted(r["FromPort"] for r in rules if "SourceSecurityGroupId" in r)
    check("app reachable only from the load balancer (8000), database only from the app (5432)",
          ports == [5432, 8000])
    _, open_bank, _ = build(allowed_cidr="", outdir=TMP / "cdk.out.closed")
    check("without ALLOWED_CIDR nobody can reach the load balancer",
          not [r for r in ingress(Template.from_stack(open_bank)) if "CidrIp" in r])
    for bad in ("0.0.0.0/0", "not-an-address"):
        try:
            Settings.load(read_env(env_file(ALLOWED_CIDR=bad)))
            check(f"ALLOWED_CIDR={bad} is refused", False)
        except ValueError:
            check(f"ALLOWED_CIDR={bad} is refused", True)

    # ---- the app task ----
    task = next(r for r in res.values() if r["Type"] == "AWS::ECS::TaskDefinition")["Properties"]
    containers = {c["Name"]: c for c in task["ContainerDefinitions"]}
    check("one task, two containers: api (web chat) and mcp (bank servers)", sorted(containers) == ["api", "mcp"])
    check("web chat waits until the bank servers are healthy",
          containers["api"]["DependsOn"] == [{"Condition": "HEALTHY", "ContainerName": "mcp"}])
    check("both containers have health checks", all("HealthCheck" in c for c in containers.values()))
    for name, c in containers.items():
        plain = {e["Name"]: e["Value"] for e in c["Environment"]}
        secret_names = {s["Name"] for s in c["Secrets"]}
        check(f"{name}: every password and key comes from Secrets Manager, none as plain settings",
              set(SECRET_KEYS) | {"PGUSER", "PGPASSWORD"} <= secret_names and not set(SECRET_KEYS) & set(plain))
        check(f"{name}: plain settings from .env are passed on", plain.get("MODEL") == PLAIN["MODEL"])
    check("database reached with PG* settings and TLS required",
          {e["Name"]: e["Value"] for e in containers["api"]["Environment"]}.get("PGSSLMODE") == "require")
    logs = next(r for r in res.values() if r["Type"] == "AWS::Logs::LogGroup")["Properties"]
    check("logs kept one week", logs["RetentionInDays"] == 7)

    out = app.synth().directory
    text = "".join(p.read_text(errors="ignore") for p in Path(out).glob("*.json"))
    check("no password or key from .env appears in any template", not any(v in text for v in SECRET_VALUES.values()))

    # ---- AWS security checks ----
    try:
        build(nag=True, outdir=TMP / "cdk.out.nag")[0].synth()
        check("AWS security checks (cdk-nag): every finding fixed or accepted with a reason", True)
    except Exception as e:  # noqa: BLE001
        print(str(e)[:3000])
        check("AWS security checks (cdk-nag): every finding fixed or accepted with a reason", False)

    secrets_script()
    print("\nALL INFRA CHECKS PASSED" if OK else "\nSOME CHECKS FAILED")
    return 0 if OK else 1


def secrets_script() -> None:
    try:
        from moto import mock_aws
    except ImportError:
        print('(secrets script checks skipped: pip install "moto[secretsmanager]" to run them)')
        return
    import boto3

    from infra import app_secrets
    env = read_env(env_file())
    os.environ.update({"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"})
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="eu-west-1")
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            app_secrets.push("eu-west-1", env)
            app_secrets.check("eu-west-1", env)
        stored = json.loads(sm.get_secret_value(SecretId="banking-agent/app")["SecretString"])
        check("push stores every key from .env in Secrets Manager", stored == SECRET_VALUES)
        check("push and check never print a value", not any(v in shown.getvalue() for v in SECRET_VALUES.values()))
        with contextlib.redirect_stdout(io.StringIO()):
            app_secrets.push("eu-west-1", {**env, "ANTHROPIC_API_KEY": "sk-ant-new"})
        stored = json.loads(sm.get_secret_value(SecretId="banking-agent/app")["SecretString"])
        check("push again updates the secret", stored["ANTHROPIC_API_KEY"] == "sk-ant-new")
        with contextlib.redirect_stdout(io.StringIO()):
            app_secrets.delete("eu-west-1", now=False)
            app_secrets.push("eu-west-1", env)
        stored = json.loads(sm.get_secret_value(SecretId="banking-agent/app")["SecretString"])
        check("push after delete brings the secret back", stored == SECRET_VALUES)
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            code = app_secrets.push("eu-west-1", {**env, "ANTHROPIC_API_KEY": ""})
        check("an empty key the app needs is flagged", code == 1 and "ANTHROPIC_API_KEY" in shown.getvalue())


if __name__ == "__main__":
    sys.exit(main())
