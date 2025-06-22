import boto3
from datetime import datetime, timedelta, timezone
import pytz
import time
import json

# --- Configuration ---
region = 'eu-north-1'
sns_topic_arn = 'arn:aws:sns:eu-north-1:135699253595:ScalingAlerts'

ami_id = 'ami-0c1ac8a41498c1a9c'
instance_type = 't3.micro'
security_group_id = 'sg-0457955da20e50a9e'
key_name = 'salamikey'
target_group_arn = 'arn:aws:elasticloadbalancing:eu-north-1:135699253595:targetgroup/TG1/2b00c3a319ad7c42'

primary_tag_key = 'Role'
primary_tag_value = 'Primary'

# --- Time setup ---
uk_tz = pytz.timezone("Europe/London")
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(minutes=5)

# --- AWS Clients ---
cloudwatch = boto3.client('cloudwatch', region_name=region)
ec2 = boto3.client('ec2', region_name=region)
sns = boto3.client('sns', region_name=region)
elbv2 = boto3.client('elbv2', region_name=region)

# --- SNS Alert ---
def send_alert(subject, message):
    sns.publish(TopicArn=sns_topic_arn, Subject=subject, Message=message)

# --- Get all running instances ---
response = ec2.describe_instances(Filters=[
    {'Name': 'instance-state-name', 'Values': ['running']}
])

all_running_instances = []
primary_instances = []

for reservation in response['Reservations']:
    for instance in reservation['Instances']:
        instance_id = instance['InstanceId']
        all_running_instances.append(instance_id)
        tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
        if tags.get(primary_tag_key) == primary_tag_value:
            primary_instances.append(instance_id)

if not all_running_instances:
    print("No running instances detected. Exiting.")
    exit()

print(f"All running instances: {all_running_instances}")
print(f"Primary (protected) instances: {primary_instances}")

# --- Monitor CPU for all running instances ---
cpu_per_instance = {}

for instance_id in all_running_instances:
    metrics = cloudwatch.get_metric_statistics(
        Namespace='AWS/EC2',
        MetricName='CPUUtilization',
        Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
        StartTime=start_time,
        EndTime=end_time,
        Period=60,
        Statistics=['Average']
    )
    datapoints = sorted(metrics['Datapoints'], key=lambda x: x['Timestamp'])

    if datapoints:
        avg_cpu = sum(p['Average'] for p in datapoints) / len(datapoints)
        timestamp_uk = datapoints[-1]['Timestamp'].astimezone(uk_tz)
        print(f"Instance {instance_id} - Timestamp (UK): {timestamp_uk.strftime('%Y-%m-%d %H:%M:%S')}, Avg CPU: {avg_cpu:.2f}%")
        cpu_per_instance[instance_id] = avg_cpu
    else:
        print(f"No CPU data for instance {instance_id}. Assuming 0%.")
        cpu_per_instance[instance_id] = 0.0

# --- Calculate overall average ---
overall_avg_cpu = sum(cpu_per_instance.values()) / len(cpu_per_instance)
print(f"\nOverall average CPU utilization: {overall_avg_cpu:.2f}%")

# --- Thresholds ---
threshold_high = 70
threshold_low = 20

# --- SCALING LOGIC ---
if overall_avg_cpu > threshold_high:
    print("SCALE UP triggered. Checking for stopped instances.")
    send_alert("SCALE UP Triggered", f"High CPU ({overall_avg_cpu:.2f}%).")

    # Check for stopped instances
    stopped_response = ec2.describe_instances(
        Filters=[{'Name': 'instance-state-name', 'Values': ['stopped']}]
    )
    stopped_instances = [
        i['InstanceId']
        for r in stopped_response['Reservations']
        for i in r['Instances']
    ]

    if stopped_instances:
        to_start = stopped_instances[0]
        print(f"Starting stopped instance: {to_start}")
        ec2.start_instances(InstanceIds=[to_start])
        time.sleep(30)
        elbv2.register_targets(TargetGroupArn=target_group_arn, Targets=[{'Id': to_start, 'Port': 80}])
        print(f"Registered {to_start} with target group.")
    else:
        print("No stopped instances found. Launching new instance.")
        ec2.run_instances(
            ImageId=ami_id,
            InstanceType=instance_type,
            KeyName=key_name,
            MaxCount=1,
            MinCount=1,
            SecurityGroupIds=[security_group_id],
            TagSpecifications=[{
                'ResourceType': 'instance',
                'Tags': [{'Key': 'Purpose', 'Value': 'ScaledInstance'}]
            }]
        )
        print("Launched new instance (will require manual setup for HTTP server).")

elif overall_avg_cpu < threshold_low:
    # Choose only instances that are NOT primary
    candidates_to_stop = [i for i in all_running_instances if i not in primary_instances]
    if candidates_to_stop:
        instance_to_stop = candidates_to_stop[-1]
        print(f"SCALE DOWN: Stopping {instance_to_stop}")
        elbv2.deregister_targets(TargetGroupArn=target_group_arn, Targets=[{'Id': instance_to_stop, 'Port': 80}])
        ec2.stop_instances(InstanceIds=[instance_to_stop])
        send_alert("SCALE DOWN Triggered", f"Low CPU ({overall_avg_cpu:.2f}%). Stopped {instance_to_stop}.")
    else:
        print("No non-primary instances to stop. Skipping.")
        send_alert("SCALE DOWN Skipped", "Only primary instances are running.")

else:
    print("NO SCALING – CPU usage within acceptable range.")

# --- Update CloudWatch Dashboard ---
def update_dashboard(instances_cpu_dict):
    widgets = []
    width = 6
    height = 6
    x, y = 0, 0

    # Per-instance CPU widgets
    for instance_id in instances_cpu_dict:
        widget = {
            "type": "metric",
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "properties": {
                "metrics": [
                    ["AWS/EC2", "CPUUtilization", "InstanceId", instance_id]
                ],
                "title": f"CPU: {instance_id}",
                "period": 60,
                "stat": "Average",
                "region": region,
                "yAxis": {"left": {"min": 0, "max": 100}},
            }
        }
        widgets.append(widget)
        x += width
        if x >= 24:
            x = 0
            y += height

    # Combined graph for all instances
    avg_widget = {
        "type": "metric",
        "x": 0,
        "y": y + height,
        "width": 24,
        "height": height,
        "properties": {
            "metrics": [
                ["AWS/EC2", "CPUUtilization", "InstanceId", instance_id]
                for instance_id in instances_cpu_dict
            ],
            "title": "Overall CPU (All Instances)",
            "period": 60,
            "stat": "Average",
            "region": region,
            "view": "timeSeries",
            "stacked": False,
            "yAxis": {"left": {"min": 0, "max": 100}}
        }
    }
    widgets.append(avg_widget)

    # --- Load Balancer Metrics ---
    elb_widgets = [
        {
            "type": "metric",
            "x": 0,
            "y": y + height * 2,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "ELB Request Count",
                "region": region,
                "metrics": [
                    ["AWS/ApplicationELB", "RequestCount", "TargetGroup", target_group_arn.split(":")[-1], "LoadBalancer", "app/my-load-balancer/50dc6c495c0c9188"]
                ],
                "stat": "Sum",
                "period": 60,
                "view": "timeSeries",
                "yAxis": {"left": {"min": 0}}
            }
        },
        {
            "type": "metric",
            "x": 12,
            "y": y + height * 2,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "ELB Latency",
                "region": region,
                "metrics": [
                    ["AWS/ApplicationELB", "Latency", "TargetGroup", target_group_arn.split(":")[-1], "LoadBalancer", "app/ScalerALB/79a143b1b78c4513"]
                ],
                "stat": "Average",
                "period": 60,
                "view": "timeSeries",
                "yAxis": {"left": {"min": 0}}
            }
        }
    ]
    widgets.extend(elb_widgets)

    dashboard_body = {
        "widgets": widgets
    }

    cloudwatch.put_dashboard(
        DashboardName='AutoScalingMonitoring',
        DashboardBody=json.dumps(dashboard_body)
    )
    print("✅ CloudWatch dashboard updated.")

# Call dashboard updater
update_dashboard(cpu_per_instance)

