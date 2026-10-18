"""BankApp: the app on AWS, open 09:00-17:00 Irish time (OPEN_HOURS in .env).

    browser --https--> CloudFront --https--> API Gateway --VPC link--> Fargate task [ api | mcp ] --> Postgres
                       opening hours,         HTTP API,     finds the task     public subnet, no NAT    isolated
                       secret header          per request   in Cloud Map       1-3 copies by CPU        subnet

- CloudFront gives the https address that Google sign-in needs. A CloudFront Function shows a
  "closed" page outside opening hours, serves the privacy notice at any hour, and passes on the
  visitor's address (stacks/opening_hours.py).
- API Gateway (HTTP API) instead of a load balancer: about $1 per million requests rather than about
  $18 a month. It reaches the task privately through a VPC link, finding it in Cloud Map.
- CloudFront adds a secret header (ORIGIN_SECRET) and the app refuses requests without it, so the
  API Gateway address can't be used to get around CloudFront.
- Opening hours: Postgres starts 30 minutes before opening (it takes a few minutes) and stops 10
  minutes after closing (EventBridge Scheduler); the app starts 15 minutes before opening and stops
  at closing (scheduled scaling). In between: 1 copy, up to APP_MAX_COPIES (3) when busy.
- No NAT gateway: the task has a public address to call Anthropic, OBP, Google and Gmail, but
  nothing can reach it except API Gateway's VPC link. The database has no route to the internet.
- Passwords and keys come from Secrets Manager (python -m infra.app_secrets push first).
"""

from __future__ import annotations

import jsii
from aws_cdk import (Acknowledgment, CfnOutput, Duration, RemovalPolicy, SecretValue, Stack, Tags, TimeZone,
                     Validations, aws_apigateway as apigateway, aws_apigatewayv2 as apigw, aws_apigatewayv2_integrations as integrations,
                     aws_applicationautoscaling as autoscaling, aws_cloudfront as cloudfront,
                     aws_cloudfront_origins as origins, aws_ec2 as ec2, aws_ecr_assets as ecr_assets, aws_ecs as ecs,
                     aws_iam as iam, aws_logs as logs, aws_rds as rds, aws_scheduler as scheduler,
                     aws_scheduler_targets as targets, aws_secretsmanager as secretsmanager,
                     aws_servicediscovery as servicediscovery)
from constructs import Construct

from settings import PROJECT, ROOT, SECRET_KEYS, SECRET_NAME, Settings
from stacks.opening_hours import clock, function_code

DB_NAME = "bank"
DB_EARLY, APP_EARLY, DB_LATE = 30, 15, 10    # minutes: database up before the app, app up before opening


@jsii.implements(apigw.IAccessLogSettings)
class _AccessLogs:
    """Where API Gateway writes one line per request, and in what format."""

    def __init__(self, destination, fmt):
        self._destination, self._format = destination, fmt

    @property
    def destination(self):
        return self._destination

    @property
    def format(self):
        return self._format


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
        zone = TimeZone.of(settings.time_zone)

        # ---------- network: 2 zones, public + isolated subnets, no NAT gateway ----------
        vpc = ec2.Vpc(self, "Network", ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/16"),
                      max_azs=2, nat_gateways=0,
                      subnet_configuration=[
                          ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                          ec2.SubnetConfiguration(name="data", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                                                  cidr_mask=24)])
        _ack(vpc, {"AwsSolutions-VPC7": "Flow logs cost money; the app's own logs cover a demo.",
                   "CloudFormation-Validate::W3010": "Zones a and b exist in every region; naming them avoids "
                                                     "asking AWS at build time."})

        link_sg = ec2.SecurityGroup(self, "LinkSG", vpc=vpc, allow_all_outbound=False,
                                    description="API Gateway VPC link: to the app task only")
        app_sg = ec2.SecurityGroup(self, "AppSG", vpc=vpc, allow_all_outbound=True,
                                   description="App task: port 8000 from the API Gateway VPC link only")
        app_sg.add_ingress_rule(link_sg, ec2.Port.tcp(8000), "from the API Gateway VPC link")
        link_sg.add_egress_rule(app_sg, ec2.Port.tcp(8000), "to the app task")
        db_sg = ec2.SecurityGroup(self, "DbSG", vpc=vpc, allow_all_outbound=False,
                                  description="Database: Postgres from the app task only")
        db_sg.add_ingress_rule(app_sg, ec2.Port.tcp(5432), "from the app task")

        # ---------- database: the smallest Postgres ----------
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
            "AwsSolutions-RDS10": "No deletion protection: cdk destroy is meant to remove everything.",
            "AwsSolutions-RDS11": "Port 5432: only the app task can reach the database, whatever the port.",
            "AwsSolutions-RDS13": "No backups: visitors' banks live 24 hours; the owner's bank data lives in OBP.",
            "AwsSolutions-SMG4": "The database password only lives as long as the stack."})

        # ---------- the app: one task, two containers (api + mcp) sharing localhost ----------
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.DISABLED)
        _ack(cluster, {"AwsSolutions-ECS4": "Container Insights costs extra; the app's logs go to CloudWatch."})
        log_group = logs.LogGroup(self, "Logs", log_group_name=f"/{PROJECT}/app",
                                  retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.DESTROY)

        image = ecs.ContainerImage.from_asset(str(ROOT), platform=ecr_assets.Platform.LINUX_AMD64,
                                              exclude=["infra", ".venv", ".git", "cdk.out", "tests"])
        task = ecs.FargateTaskDefinition(   # the smallest size that fits both: 0.25 vCPU, 1 GB
            self, "Task", cpu=256, memory_limit_mib=1024,
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

        # ---------- the way in: API Gateway -> VPC link -> Cloud Map -> the task ----------
        namespace = servicediscovery.PrivateDnsNamespace(self, "Namespace", name=f"{PROJECT}.internal", vpc=vpc)
        registry = namespace.create_service(
            "AppService", name="app", dns_record_type=servicediscovery.DnsRecordType.SRV,  # address and port
            dns_ttl=Duration.seconds(10), custom_health_check=servicediscovery.HealthCheckCustomConfig(failure_threshold=1))
        vpc_link = apigw.VpcLink(self, "Link", vpc=vpc, security_groups=[link_sg],
                                 subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC))
        api_gw = apigw.HttpApi(
            self, "Api", api_name=f"{PROJECT}-api", create_default_stage=False,
            description="Passes requests from CloudFront to the app task",
            default_integration=integrations.HttpServiceDiscoveryIntegration(
                "App", registry, vpc_link=vpc_link, timeout=Duration.seconds(29)))
        api_logs = logs.LogGroup(self, "ApiLogs", retention=logs.RetentionDays.ONE_WEEK,
                                 removal_policy=RemovalPolicy.DESTROY)
        apigw.HttpStage(
            self, "Stage", http_api=api_gw, stage_name="$default", auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=50, burst_limit=100),   # a cap on runaway costs
            access_log_settings=_AccessLogs(apigw.LogGroupLogDestination(api_logs), apigateway.AccessLogFormat.custom(
                '{"id":"$context.requestId","time":"$context.requestTime","method":"$context.httpMethod","path":"$context.path",'
                '"status":"$context.status","ms":"$context.responseLatency",'
                '"error":"$context.integrationErrorMessage"}')))
        _ack(api_gw, {"AwsSolutions-APIG4": "Sign-in is done by the app itself (Google or an emailed code), and "
                                            "only requests carrying CloudFront's secret header get through."})

        # ---------- CloudFront: https, opening hours, the secret header ----------
        privacy = (ROOT / "web" / "privacy.html").read_text(encoding="utf-8").replace("__CONTACT__", settings.contact_email)
        gate = cloudfront.Function(self, "OpeningHours",
                                   code=cloudfront.FunctionCode.from_inline(function_code(settings.hours, privacy)),
                                   runtime=cloudfront.FunctionRuntime.JS_2_0,
                                   comment="Closed page outside opening hours; the visitor's address")
        # A reference to the secret, resolved by CloudFormation when deploying: the value is not in the template.
        origin_secret = SecretValue.secrets_manager(SECRET_NAME, json_field="ORIGIN_SECRET").unsafe_unwrap()
        site = cloudfront.Distribution(
            self, "Site", comment=f"{PROJECT}: the public address",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100, http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.HttpOrigin(
                    f"{api_gw.api_id}.execute-api.{self.region}.{self.url_suffix}",
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                    custom_headers={"X-Origin-Verify": origin_secret}, read_timeout=Duration.seconds(30)),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,   # every answer is personal
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                function_associations=[cloudfront.FunctionAssociation(
                    function=gate, event_type=cloudfront.FunctionEventType.VIEWER_REQUEST)]))
        _ack(site, {
            "AwsSolutions-CFR1": "Open to every country: it's a public demo.",
            "AwsSolutions-CFR3": "Access logs need a bucket; API Gateway and the app log every request.",
            "AwsSolutions-CFR4": "The default cloudfront.net certificate can't set a minimum TLS version; "
                                 "a custom domain would.",
        })
        _ack(site, {"AwsSolutions-CFR2": "No AWS WAF (it would cost about $9 a month). The app limits sign-ups, "
                                         "messages, codes and spending per visitor, and API Gateway caps requests."})
        public_url = f"https://{site.distribution_domain_name}"

        # ---------- the containers ----------
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
                start_period=Duration.seconds(60))   # a quarter of a CPU takes a while to load the agents

        mcp = task.add_container(
            "mcp", image=image, command=["python", "-m", "mcp_servers.run"], environment=environment,
            secrets=secrets, health_check=health(8104),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="mcp", log_group=log_group))
        api = task.add_container(
            "api", image=image, secrets=secrets, health_check=health(8000),
            environment={**environment, "API_HOST": "0.0.0.0", "PORT": "8000",
                         "AUTH_MODE": "public", "PUBLIC_URL": public_url, "COOKIE_SECURE": "1",
                         "OPEN_TO_PUBLIC": "1" if settings.open_to_public else "0",
                         "CLIENT_IP_HEADER": "X-Viewer-Ip"},   # set by the CloudFront Function
            port_mappings=[ecs.PortMapping(container_port=8000)],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="api", log_group=log_group))
        api.add_container_dependencies(
            ecs.ContainerDependency(container=mcp, condition=ecs.ContainerDependencyCondition.HEALTHY))

        service = ecs.FargateService(
            self, "Service", cluster=cluster, task_definition=task,
            assign_public_ip=True, vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[app_sg], min_healthy_percent=100, max_healthy_percent=200,  # new copy first
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True))
        service.associate_cloud_map_service(service=registry, container=api, container_port=8000)

        # ---------- size and opening hours ----------
        # At least 1 copy while open (scaling on CPU never goes to 0); closing time sets both limits to 0.
        copies = service.auto_scale_task_count(min_capacity=1, max_capacity=settings.max_copies)
        copies.scale_on_cpu_utilization("Busy", target_utilization_percent=60,
                                        scale_out_cooldown=Duration.minutes(1), scale_in_cooldown=Duration.minutes(5))
        if settings.hours:
            opens, closes = settings.hours
            app_up, db_up, db_down = opens - APP_EARLY, opens - DB_EARLY, closes + DB_LATE

            def cron(minutes: int) -> autoscaling.Schedule:
                return autoscaling.Schedule.cron(hour=str(minutes // 60), minute=str(minutes % 60))

            copies.scale_on_schedule("Open", schedule=cron(app_up), time_zone=zone,
                                     min_capacity=1, max_capacity=settings.max_copies)
            copies.scale_on_schedule("Close", schedule=cron(closes), time_zone=zone, min_capacity=0, max_capacity=0)

            def database(name: str, action: str, minutes: int) -> None:
                scheduler.Schedule(
                    self, name, description=f"{action} the database at {clock(minutes)} {settings.time_zone}",
                    schedule=scheduler.ScheduleExpression.cron(hour=str(minutes // 60), minute=str(minutes % 60),
                                                               time_zone=zone),
                    target=targets.Universal(
                        service="rds", action=action, retry_attempts=3,
                        input=scheduler.ScheduleTargetInput.from_object({"DbInstanceIdentifier": db.instance_identifier}),
                        policy_statements=[iam.PolicyStatement(actions=[f"rds:{action[0].upper()}{action[1:]}"],
                                                               resources=[db.instance_arn])]))

            database("DatabaseStart", "startDBInstance", db_up)
            database("DatabaseStop", "stopDBInstance", db_down)

        # ---------- what to open ----------
        CfnOutput(self, "AppUrl", value=public_url, description="The app's address")
        CfnOutput(self, "GoogleRedirectUri", value=f"{public_url}/api/auth/google/callback",
                  description="Add this to your Google client's Authorized redirect URIs")
        CfnOutput(self, "PrivacyUrl", value=f"{public_url}/privacy.html",
                  description="For Google Auth Platform > Branding > Privacy policy link")
        CfnOutput(self, "Hours", description="When the app is open",
                  value=(f"{clock(settings.hours[0])}-{clock(settings.hours[1])} {settings.time_zone}"
                         if settings.hours else "always"))
        CfnOutput(self, "LogGroup", value=log_group.log_group_name,
                  description="aws logs tail /banking-agent/app --follow")
