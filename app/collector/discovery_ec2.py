#app/collector/discovery_ec2.py
from app.db import get_connection
from app.aws.sts import get_boto3_session
import boto3
import json

def discover_ec2():
    """
    Discover EC2 + attached resources. Session resolution (static keys,
    cross-account AssumeRole, or same-account default) is handled by the
    single choke point in app.aws.sts.get_boto3_session().
    """

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM aws_accounts
        WHERE status = 'active'
    """)
    accounts = cursor.fetchall()

    if not accounts:
        print("No active AWS accounts found.")
        return

    for account in accounts:
        print(f"Discovering EC2 in account: {account['account_name']}")

        # -------------------------
        # SESSION SELECTION
        # -------------------------
        # Previously hardcoded to check account_id == this instance's own
        # account id -- redundant with (and could drift from) the generic
        # same-account short-circuit already in app.aws.sts.assume_role()
        # (fix: 22ff060), and never handled static_keys accounts at all.
        # get_boto3_session() is the one place this logic should live.
        try:
            session = get_boto3_session(account)
        except Exception as e:
            print(f"Session failed for {account['account_name']}: {e}")
            continue

        region = account.get("default_region")
        ec2 = session.client("ec2", region_name=region)
        paginator = ec2.get_paginator("describe_instances")

        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):

                    instance_id = instance["InstanceId"]

                    # -------------------------
                    # Normalize tags
                    # -------------------------
                    raw_tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}

                    env = (
                        raw_tags.get("environment")
                        or raw_tags.get("Environment")
                        or raw_tags.get("ENVIRONMENT")
                    )

                    tags = dict(raw_tags)
                    if env:
                        tags["environment"] = env.lower()

                    name = raw_tags.get("Name")

                    # -------------------------
                    # EC2 INSERT
                    # -------------------------
                    cursor.execute("""
                        INSERT INTO resources
                            (aws_account_id, resource_type, resource_id, name, tags)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            name = VALUES(name),
                            tags = VALUES(tags)
                    """, (
                        account["id"],
                        "ec2",
                        instance_id,
                        name,
                        json.dumps(tags),
                    ))

                    print("  EC2:", instance_id, name)

                    # -------------------------
                    # EBS INHERITANCE
                    # -------------------------
                    for mapping in instance.get("BlockDeviceMappings", []):
                        ebs = mapping.get("Ebs")
                        if not ebs:
                            continue

                        volume_id = ebs["VolumeId"]

                        inherited_tags = dict(tags)
                        inherited_tags["parent_ec2"] = instance_id

                        cursor.execute("""
                            INSERT INTO resources
                                (aws_account_id, resource_type, resource_id, name, tags)
                            VALUES (%s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                tags = VALUES(tags)
                        """, (
                            account["id"],
                            "ebs",
                            volume_id,
                            volume_id,
                            json.dumps(inherited_tags),
                        ))

                    # -------------------------
                    # ENI INHERITANCE
                    # -------------------------
                    for eni in instance.get("NetworkInterfaces", []):
                        eni_id = eni["NetworkInterfaceId"]

                        inherited_tags = dict(tags)
                        inherited_tags["parent_ec2"] = instance_id

                        cursor.execute("""
                            INSERT INTO resources
                                (aws_account_id, resource_type, resource_id, name, tags)
                            VALUES (%s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                tags = VALUES(tags)
                        """, (
                            account["id"],
                            "eni",
                            eni_id,
                            eni_id,
                            json.dumps(inherited_tags),
                        ))

    conn.commit()
    cursor.close()
    conn.close()

    print("EC2 discovery completed.")