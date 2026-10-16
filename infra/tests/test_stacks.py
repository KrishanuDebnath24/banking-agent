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
import re
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
         "CUSTOMER_A_USERNAME": "robert.uk.29@example.com", "EMAIL_BACKEND": "smtp",
         "OWNER_EMAILS": "owner@example.test"}


def check(label, cond):
    global OK
    OK &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label)


def env_file(**extra) -> Path:
    path = TMP / f"{len(list(TMP.iterdir()))}.env"
    path.write_text("\n".join(f"{k}={v}" for k, v in {**PLAIN, **SECRET_VALUES, **extra}.items()) + "\n")
    return path


def build(nag=False, outdir=None, **env):
    settings = Settings.load(read_env(env_file(AWS_REGION="eu-west-1", **env)))
    settings.account = "123456789012"
    app = cdk.App(outdir=str(outdir or TMP / "cdk.out"))
    if nag:
        from cdk_nag import AwsSolutionsChecks
        cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))
    bank = BankStack(app, "BankApp", env=ENV, settings=settings)
    budget = BudgetStack(app, "BankBudget", env=ENV, limit_usd=25, email="alerts@example.test")
    return app, bank, budget


def only(resources: dict, kind: str) -> dict:
    """The properties of the one resource of this type."""
    found = [r["Properties"] for r in resources.values() if r["Type"] == kind]
    assert len(found) == 1, f"{len(found)} x {kind}"
    return found[0]


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
    check("nothing on the internet can reach the task or the database directly",
          not [r for r in rules if "CidrIp" in r or "CidrIpv6" in r])
    # EC2 only accepts these characters in security group descriptions (an apostrophe broke a deploy).
    allowed = re.compile(r"^[a-zA-Z0-9. _\-:/()#,@\[\]+=&;{}!$*]*$")
    texts = [r["Properties"]["GroupDescription"] for r in res.values() if r["Type"] == "AWS::EC2::SecurityGroup"]
    texts += [r.get("Description", "") for r in rules]
    texts += [r.get("Description", "") for res_ in res.values() if res_["Type"] == "AWS::EC2::SecurityGroupEgress"
              for r in [res_["Properties"]]]
    bad = [t for t in texts if not allowed.match(t)]
    check("security group descriptions only use characters EC2 accepts", not bad)
    if bad:
        print("   ", bad)
    ports = sorted(r["FromPort"] for r in rules if "SourceSecurityGroupId" in r)
    check("app reachable only from API Gateway's VPC link (8000), database only from the app (5432)",
          ports == [5432, 8000])
    check("no load balancer (API Gateway is pay-per-request)",
          not any(r["Type"].startswith("AWS::ElasticLoadBalancing") for r in res.values()))
    integration = only(res, "AWS::ApiGatewayV2::Integration")
    registry = only(res, "AWS::ServiceDiscovery::Service")
    check("API Gateway reaches the task privately: VPC link, then Cloud Map (address and port)",
          integration["ConnectionType"] == "VPC_LINK" and integration["IntegrationType"] == "HTTP_PROXY"
          and "AppService" in integration["IntegrationUri"]["Fn::GetAtt"][0]
          and registry["DnsConfig"]["DnsRecords"][0]["Type"] == "SRV")
    stage = only(res, "AWS::ApiGatewayV2::Stage")
    check("API Gateway: requests capped (50 a second) and logged",
          stage["DefaultRouteSettings"]["ThrottlingRateLimit"] == 50 and "AccessLogSettings" in stage)

    site = only(res, "AWS::CloudFront::Distribution")["DistributionConfig"]
    behavior = site["DefaultCacheBehavior"]
    origin = site["Origins"][0]
    check("CloudFront: https only, nothing cached (every answer is personal), API Gateway behind it",
          behavior["ViewerProtocolPolicy"] == "redirect-to-https"
          and behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
          and origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
          and ".execute-api." in json.dumps(origin["DomainName"]))
    header = origin["OriginCustomHeaders"][0]
    check("CloudFront adds the secret header, taken from Secrets Manager at deploy time (not in the template)",
          header["HeaderName"] == "X-Origin-Verify"
          and header["HeaderValue"] == "{{resolve:secretsmanager:banking-agent/app:SecretString:ORIGIN_SECRET::}}")
    gate = only(res, "AWS::CloudFront::Function")
    check("the opening-hours function runs on every request", behavior["FunctionAssociations"][0]["EventType"]
          == "viewer-request" and "var OPEN = 540, CLOSE = 1020" in gate["FunctionCode"])
    check("... serves the privacy notice at any hour, with the contact address, within CloudFront's 10 KB",
          "Privacy notice" in gate["FunctionCode"] and "mailto:owner@example.test" in gate["FunctionCode"]
          and len(gate["FunctionCode"].encode()) <= 10240 and "PrivacyUrl" in t.to_json()["Outputs"])

    # ---- opening hours ----
    scaling = only(res, "AWS::ApplicationAutoScaling::ScalableTarget")
    actions = {a["ScheduledActionName"]: a for a in scaling["ScheduledActions"]}
    check("app: 1 to 3 copies by CPU, starting 08:45 and stopping 17:00 Irish time",
          scaling["MinCapacity"] == 1 and scaling["MaxCapacity"] == 3
          and actions["Open"]["Schedule"] == "cron(45 8 * * ? *)" and actions["Open"]["Timezone"] == "Europe/Dublin"
          and actions["Open"]["ScalableTargetAction"] == {"MinCapacity": 1, "MaxCapacity": 3}
          and actions["Close"]["Schedule"] == "cron(0 17 * * ? *)"
          and actions["Close"]["ScalableTargetAction"] == {"MinCapacity": 0, "MaxCapacity": 0})
    schedules = {json.dumps(p["Target"]["Arn"]).split(":")[-1].rstrip('"]}'): p
                 for p in (r["Properties"] for r in res.values() if r["Type"] == "AWS::Scheduler::Schedule")}
    check("database: started 08:30 and stopped 17:10 Irish time",
          schedules["startDBInstance"]["ScheduleExpression"] == "cron(30 8 * * ? *)"
          and schedules["stopDBInstance"]["ScheduleExpression"] == "cron(10 17 * * ? *)"
          and all(p["ScheduleExpressionTimezone"] == "Europe/Dublin" for p in schedules.values()))
    policies = [r["Properties"]["PolicyDocument"]["Statement"] for r in res.values() if r["Type"] == "AWS::IAM::Policy"]
    db_actions = [st for sts in policies for st in sts if str(st["Action"]).startswith("rds:")]
    check("the schedules may only start and stop this one database",
          sorted(st["Action"] for st in db_actions) == ["rds:StartDBInstance", "rds:StopDBInstance"]
          and all("DatabaseB269D8BB" in json.dumps(st["Resource"]) for st in db_actions))
    _, always, _ = build(outdir=TMP / "cdk.out.always", OPEN_HOURS="always", APP_MAX_COPIES="2")
    ta = Template.from_stack(always).to_json()["Resources"]
    check("OPEN_HOURS=always: no schedules, always open",
          not [r for r in ta.values() if r["Type"] == "AWS::Scheduler::Schedule"]
          and "ScheduledActions" not in only(ta, "AWS::ApplicationAutoScaling::ScalableTarget")
          and only(ta, "AWS::ApplicationAutoScaling::ScalableTarget")["MaxCapacity"] == 2
          and "var OPEN = -1" in only(ta, "AWS::CloudFront::Function")["FunctionCode"])
    for bad in ("9-5", "17:00-09:00", "00:00-12:00"):
        try:
            Settings.load(read_env(env_file(OPEN_HOURS=bad)))
            check(f"OPEN_HOURS={bad} is refused", False)
        except ValueError:
            check(f"OPEN_HOURS={bad} is refused", True)

    # ---- the app task ----
    task = next(r for r in res.values() if r["Type"] == "AWS::ECS::TaskDefinition")["Properties"]
    containers = {c["Name"]: c for c in task["ContainerDefinitions"]}
    check("the smallest task size: a quarter of a CPU, 1 GB", task["Cpu"] == "256" and task["Memory"] == "1024")
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
    api_env = {e["Name"]: e["Value"] for e in containers["api"]["Environment"]}
    check("database reached with PG* settings and TLS required", api_env.get("PGSSLMODE") == "require")
    check("web chat reads the visitor's address from the header the CloudFront Function sets",
          api_env.get("CLIENT_IP_HEADER") == "X-Viewer-Ip")
    check("real sign-in on the https address, secure cookies, invite-only until OPEN_TO_PUBLIC=1",
          api_env.get("AUTH_MODE") == "public" and api_env.get("COOKIE_SECURE") == "1"
          and api_env.get("OPEN_TO_PUBLIC") == "0" and "https://" in json.dumps(api_env.get("PUBLIC_URL")))
    check("APP_SECRET (signs visitors' bank tokens) comes from Secrets Manager, for both containers",
          all("APP_SECRET" in {s["Name"] for s in c["Secrets"]} for c in containers.values()))
    check("logs kept one week", all(r["Properties"]["RetentionInDays"] == 7
                                    for r in res.values() if r["Type"] == "AWS::Logs::LogGroup"))

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
        no_origin = {k: v for k, v in env.items() if k != "ORIGIN_SECRET"}
        with contextlib.redirect_stdout(io.StringIO()):
            app_secrets.delete("eu-west-1", now=True)
            app_secrets.push("eu-west-1", no_origin)
            first = json.loads(sm.get_secret_value(SecretId="banking-agent/app")["SecretString"])["ORIGIN_SECRET"]
            app_secrets.push("eu-west-1", no_origin)
            again = json.loads(sm.get_secret_value(SecretId="banking-agent/app")["SecretString"])["ORIGIN_SECRET"]
        check("CloudFront's secret header is made for you once, then kept", len(first) >= 32 and first == again)
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            code = app_secrets.push("eu-west-1", {**env, "ANTHROPIC_API_KEY": ""})
        check("an empty key the app needs is flagged", code == 1 and "ANTHROPIC_API_KEY" in shown.getvalue())


if __name__ == "__main__":
    sys.exit(main())
