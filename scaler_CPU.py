import boto3
from datetime import datetime, timedelta, timezone
import pytz

# --- Configuration ---
instance_id = 'i-0851a236955f56eb9'
region = 'eu-north-1'
sns_topic_arn = 'arn:aws:sns:eu-north-1:135699253595:ScalingAlerts'

# Launch config
ami_id = 'ami-0c1ac8a41498c1a9c'
instance_type = 't3.micro'
security_group_id = 'sg-0457955da20e50a9e'
key_name = 'salamikey'

# --- Time setup ---
uk_tz = pytz.timezone("Europe/London")
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(minutes=5)

# --- AWS Clients ---
cloudwatch = boto3.client('cloudwatch', region_name=region)
ec2 = boto3.client('ec2', region_name=region)
sns = boto3.client('sns', region_name=region)

# --- SNS Alert ---
def send_alert(subject, message):
    sns.publish(TopicArn=sns_topic_arn, Subject=subject, Message=message)

# --- CPU Monitoring ---
response = cloudwatch.get_metric_statistics(
    Namespace='AWS/EC2',
    MetricName='CPUUtilization',
    Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
    StartTime=start_time,
    EndTime=end_time,
    Period=60,
    Statistics=['Average']
)

datapoints = sorted(response['Datapoints'], key=lambda x: x['Timestamp'])

for point in datapoints:
    timestamp_uk = point['Timestamp'].astimezone(uk_tz)
    print(f"Timestamp (UK): {timestamp_uk.strftime('%Y-%m-%d %H:%M:%S')}, Avg CPU Utilization: {point['Average']:.2f}%")

# --- Thresholds ---
threshold_high = 70
threshold_low = 20

# --- Scaling Decision ---
if datapoints:
    avg_cpu = sum(point['Average'] for point in datapoints) / len(datapoints)
    print(f"\nAverage CPU utilization over period: {avg_cpu:.2f}%")

    # Get running instances
    response = ec2.describe_instances(
        Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
    )
    running_instances = [
        instance['InstanceId']
        for reservation in response['Reservations']
        for instance in reservation['Instances']
    ]
    print(f"Currently running instances: {running_instances}")

    if avg_cpu > threshold_high:
        print("Decision: SCALE UP – CPU usage is high. Launching a new instance.")
        send_alert(
            "SCALE UP Triggered",
            f"High CPU usage detected ({avg_cpu:.2f}%). Launching a new EC2 instance."
        )

        # --- Launch EC2 instance ---
        ec2.run_instances(
            ImageId=ami_id,
            InstanceType=instance_type,
            KeyName=key_name,
            MaxCount=1,
            MinCount=1,
            SecurityGroupIds=[security_group_id],
            TagSpecifications=[
                {
                    'ResourceType': 'instance',
                    'Tags': [{'Key': 'Purpose', 'Value': 'ScaledInstance'}]
                }
            ]
        )

    elif avg_cpu < threshold_low:
        if len(running_instances) > 1:
            instance_to_stop = running_instances[-1]
            print(f"Decision: SCALE DOWN – CPU usage is low. Stopping instance {instance_to_stop}.")
            ec2.stop_instances(InstanceIds=[instance_to_stop])
            send_alert(
                "SCALE DOWN Triggered",
                f"Low CPU usage detected ({avg_cpu:.2f}%). Stopping instance {instance_to_stop}."
            )
        else:
            print("Only one instance running; skipping stop to avoid downtime.")
            send_alert(
                "SCALE DOWN Skipped",
                f"CPU is low ({avg_cpu:.2f}%) but only one instance running. No action taken."
            )
    else:
        print("Decision: NO SCALING – CPU usage is within acceptable range.")
else:
    print("No datapoints available to make scaling decision.")
