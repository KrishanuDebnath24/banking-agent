"""BankApp: the whole app on AWS, created before a demo and deleted after it (cdk destroy).

    your browser --80--> load balancer --8000--> Fargate task [ api | mcp ] --5432--> RDS Postgres
                         (only ALLOWED_CIDR)     (public subnet, no NAT)          (isolated subnet)

- One Fargate task runs the image twice: the web chat (api) and the bank servers (mcp). They share
  localhost, exactly like docker compose, so the bank servers are never reachable from outside.
- No NAT gateway (the expensive part of most AWS networks): the task has a public IP for calling the
  OBP sandbox, Anthropic, Gmail and AWS itself; nothing can call in except through the load balancer.
- The database sits in subnets with no route to the internet; only the app task may connect.
- Passwords and API keys come from Secrets Manager at start-up (python -m infra.secrets push first).
- Until sign-in (Cognito) and HTTPS (CloudFront) arrive in steps 4-5, only your own address
  (ALLOWED_CIDR in .env) can open the app, over plain http.
"""

from __future__ import annotations

from aws_cdk import (Acknowledgment, CfnOutput, Duration, RemovalPolicy, Stack, Tags, Validations,
                     aws_ec2 as ec2, aws_ecr_assets as ecr_assets, aws_ecs as ecs,
                     aws_elasticloadbalancingv2 as elbv2, aws_iam as iam, aws_logs as logs, aws_rds as rds,
                     aws_secretsmanager as secretsmanager)
from constructs import Construct

from settings import PROJECT, ROOT, SECRET_KEYS, SECRET_NAME, Settings

DB_NAME = "bank"


def _ack(construct, reasons: dict[str, str]) -> None:
    """Record why a security-check finding is accepted for a demo (shown in the cdk-nag report,
    see infra/tests). Applies to the construct and everything inside it."""
    for rule, reason in reasons.items():
        Validations.of(construct).acknowledge(Acknowledgment(id=rule, reason=reason))


class BankStack(Stack):
    @property
    def availability_zones(self) -> list[str]:
        # Two zones named directly ("a" and "b" exist in every region), so building the templates
        # never needs to ask AWS which zones exist.
        return [f"{self.region}a", f"{self.region}b"]

    def __init__(self, scope: Construct, construct_id: str, *, settings: Settings, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", PROJECT)

        # ---------- network: 2 zones, public + isolated subnets, no NAT gateway ----------
        vpc = ec2.Vpc(self, "Network", ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/16"),
                      max_azs=2, nat_gateways=0,
                      subnet_configuration=[
                          ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                          ec2.SubnetConfiguration(name="data", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                                                  cidr_mask=24)])
        _ack(vpc, {"AwsSolutions-VPC7": "Flow logs cost money and this VPC only lives for a demo.",
                   "CloudFormation-Validate::W3010": "Zones a and b exist in every region; naming them avoids "
                                                     "asking AWS at build time."})

        web_sg = ec2.SecurityGroup(self, "WebSG", vpc=vpc, allow_all_outbound=False,
                                   description="Load balancer: http from ALLOWED_CIDR only")
        if settings.allowed_cidr:
            web_sg.add_ingress_rule(ec2.Peer.ipv4(settings.allowed_cidr), ec2.Port.tcp(80), "ALLOWED_CIDR in .env")
        app_sg = ec2.SecurityGroup(self, "AppSG", vpc=vpc, allow_all_outbound=True,
                                   description="App task: port 8000 from the load balancer only")
        app_sg.add_ingress_rule(web_sg, ec2.Port.tcp(8000), "from the load balancer")
        web_sg.add_egress_rule(app_sg, ec2.Port.tcp(8000), "to the app task")
        db_sg = ec2.SecurityGroup(self, "DbSG", vpc=vpc, allow_all_outbound=False,
                                  description="Database: Postgres from the app task only")
        db_sg.add_ingress_rule(app_sg, ec2.Port.tcp(5432), "from the app task")

        # ---------- database: the smallest Postgres, deleted with the stack ----------
        db = rds.DatabaseInstance(
            self, "Database",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.of("17", "17")),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO),
            vpc=vpc, vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[db_sg], credentials=rds.Credentials.from_generated_secret("bank"),
            database_name=DB_NAME, allocated_storage=20, storage_type=rds.StorageType.GP3,
            storage_encrypted=True, multi_az=False, publicly_accessible=False,
            backup_retention=Duration.days(0), delete_automated_backups=True,
            deletion_protection=False, removal_policy=RemovalPolicy.DESTROY,
            auto_minor_version_upgrade=True, enable_performance_insights=False)
        _ack(db, {
            "AwsSolutions-RDS3": "Single zone: a demo doesn't need a standby copy (it would double the cost).",
            "AwsSolutions-RDS10": "No deletion protection: the database is deleted after every demo on purpose.",
            "AwsSolutions-RDS11": "Port 5432: only the app task can reach the database, whatever the port.",
            "AwsSolutions-RDS13": "No backups: demo data is recreated each time; the bank's data lives in OBP.",
            "AwsSolutions-SMG4": "The database password only lives as long as the demo stack."})

        # ---------- the app: one task, two containers (api + mcp) sharing localhost ----------
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.DISABLED)
        _ack(cluster, {"AwsSolutions-ECS4": "Container Insights costs extra; the app's logs go to CloudWatch."})
        log_group = logs.LogGroup(self, "Logs", log_group_name=f"/{PROJECT}/app",
                                  retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.DESTROY)

        image = ecs.ContainerImage.from_asset(str(ROOT), platform=ecr_assets.Platform.LINUX_AMD64,
                                              exclude=["infra", ".venv", ".git", "cdk.out", "tests"])
        task = ecs.FargateTaskDefinition(
            self, "Task", cpu=512, memory_limit_mib=2048,
            runtime_platform=ecs.RuntimePlatform(cpu_architecture=ecs.CpuArchitecture.X86_64,
                                                 operating_system_family=ecs.OperatingSystemFamily.LINUX))
        # Bedrock models and the Bedrock guardrail, if .env uses them (the task's own role: no keys needed).
        task.task_role.add_to_principal_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:ApplyGuardrail"],
            resources=["arn:aws:bedrock:*::foundation-model/*",
                       f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                       f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/*"]))
        bedrock_why = "The model is chosen in .env, and EU inference profiles run in several EU regions."
        _ack(task, {
            "AwsSolutions-ECS2": "Plain settings (model name, bank URL...) are environment variables; "
                                 "passwords and keys come from Secrets Manager.",
            "AwsSolutions-IAM5[Resource::arn:aws:bedrock:*::foundation-model/*]": bedrock_why,
            f"AwsSolutions-IAM5[Resource::arn:aws:bedrock:*:{self.account}:inference-profile/*]": bedrock_why,
            f"AwsSolutions-IAM5[Resource::arn:aws:bedrock:{self.region}:{self.account}:guardrail/*]":
                "Whichever guardrail GUARDRAIL_ID names, in this account and region.",
            "AwsSolutions-IAM5[Resource::*]": "Pulling the image needs ecr:GetAuthorizationToken, which AWS "
                                              "only allows on *."})

        app_secret = secretsmanager.Secret.from_secret_name_v2(self, "AppSecret", SECRET_NAME)
        secrets = {key: ecs.Secret.from_secrets_manager(app_secret, key) for key in SECRET_KEYS}
        secrets["PGUSER"] = ecs.Secret.from_secrets_manager(db.secret, "username")
        secrets["PGPASSWORD"] = ecs.Secret.from_secrets_manager(db.secret, "password")
        environment = {
            **settings.app_env,
            # "postgresql://" with no address: the Postgres driver takes host, user and password from PG*.
            "DATABASE_URL": "postgresql://", "PGHOST": db.db_instance_endpoint_address,
            "PGPORT": db.db_instance_endpoint_port, "PGDATABASE": DB_NAME, "PGSSLMODE": "require",
            "AWS_REGION": self.region, "PYTHONUNBUFFERED": "1",
        }

        def health(port: int) -> ecs.HealthCheck:
            return ecs.HealthCheck(
                command=["CMD", "python", "-c",
                         f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/healthz', timeout=3)"],
                interval=Duration.seconds(10), timeout=Duration.seconds(5), retries=6,
                start_period=Duration.seconds(30))

        mcp = task.add_container(
            "mcp", image=image, command=["python", "-m", "mcp_servers.run"], environment=environment,
            secrets=secrets, health_check=health(8104),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="mcp", log_group=log_group))
        api = task.add_container(
            "api", image=image, secrets=secrets, health_check=health(8000),
            environment={**environment, "API_HOST": "0.0.0.0", "PORT": "8000",
                         "AUTH_MODE": "dev", "COOKIE_SECURE": "0"},   # sign-in and https: steps 4-5
            port_mappings=[ecs.PortMapping(container_port=8000)],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="api", log_group=log_group))
        api.add_container_dependencies(
            ecs.ContainerDependency(container=mcp, condition=ecs.ContainerDependencyCondition.HEALTHY))

        service = ecs.FargateService(
            self, "Service", cluster=cluster, task_definition=task, desired_count=1,
            assign_public_ip=True, vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[app_sg], min_healthy_percent=0, max_healthy_percent=100,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            health_check_grace_period=Duration.seconds(120))

        # ---------- the way in: a load balancer only your address can reach (for now) ----------
        alb = elbv2.ApplicationLoadBalancer(
            self, "Web", vpc=vpc, internet_facing=True, security_group=web_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            idle_timeout=Duration.seconds(120), drop_invalid_header_fields=True)
        _ack(alb, {"AwsSolutions-ELB2": "Access logs need an S3 bucket kept between demos; app logs cover it."})
        listener = alb.add_listener("Http", port=80, open=False)
        listener.add_targets(
            "App", port=8000, protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service.load_balancer_target(container_name="api", container_port=8000)],
            health_check=elbv2.HealthCheck(path="/healthz", healthy_http_codes="200",
                                           interval=Duration.seconds(15), healthy_threshold_count=2),
            deregistration_delay=Duration.seconds(10))

        CfnOutput(self, "AppUrl", value=f"http://{alb.load_balancer_dns_name}",
                  description="Open this (from ALLOWED_CIDR only)")
        CfnOutput(self, "LogGroup", value=log_group.log_group_name,
                  description="aws logs tail /banking-agent/app --follow")
