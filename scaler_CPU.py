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

uk_tz = pytz.timezone("Europe/London")
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(minutes=5)

cloudwatch = boto3.client('cloudwatch', region_name=region)
ec2 = boto3.client('ec2', region_name=region)
sns = boto3.client('sns', region_name=region)
elbv2 = boto3.client('elbv2', region_name=region)

def send_alert(subject, message):
    sns.publish(TopicArn=sns_topic_arn, Subject=subject, Message=message)

def wait_for_instance_ok(instance_id, timeout=300, interval=15):
    """Wait for instance status to be 'ok' and running"""
    print(f"Waiting for instance {instance_id} to be running and status OK...")
    waiter = ec2.get_waiter('instance_running')
    try:
        waiter.wait(InstanceIds=[instance_id], WaiterConfig={'Delay': interval, 'MaxAttempts': int(timeout/interval)})
    except Exception as e:
        print(f"Error waiting for instance to run: {e}")
        return False

    # Now wait for system status checks to pass
    for _ in range(int(timeout/interval)):
        statuses = ec2.describe_instance_status(InstanceIds=[instance_id])
        if statuses['InstanceStatuses']:
            status = statuses['InstanceStatuses'][0]
            sys_status = status['SystemStatus']['Status']
            inst_status = status['InstanceStatus']['Status']
            if sys_status == 'ok' and inst_status == 'ok':
                print(f"Instance {instance_id} is running and status OK.")
                return True
        time.sleep(interval)

    print(f"Timeout waiting for instance {instance_id} status OK.")
    return False

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

overall_avg_cpu = sum(cpu_per_instance.values()) / len(cpu_per_instance)
print(f"\nOverall average CPU utilization: {overall_avg_cpu:.2f}%")

threshold_high = 60
threshold_low = 20

if overall_avg_cpu > threshold_high:
    print("SCALE UP triggered. Checking for stopped instances.")
    send_alert("SCALE UP Triggered", f"High CPU ({overall_avg_cpu:.2f}%).")

    stopped_response = ec2.describe_instances(
        Filters=[{'Name': 'instance-state-name', 'Values': ['stopped']}]
    )
    stopped_instances = [
        i['InstanceId']
        for r in stopped_response['Reservations']
        for i in r['Instances']
    ]

    user_data_script = '''#!/bin/bash
cd /home/ubuntu

# Update and install dependencies
apt update -y
apt install -y python3-venv

# Set up virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Flask inside the venv
/home/ubuntu/venv/bin/pip install flask

# Create Flask app
cat > app.py <<EOF
from flask import Flask
import time

app = Flask(__name__)

@app.route("/health")
def health_check():
    return "OK", 200

@app.route("/")
def cpu_burner():
    start = time.time()
    while time.time() - start < 0.5:
        pass
    return "CPU-intensive response"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
EOF

# Run Flask app in background
nohup /home/ubuntu/venv/bin/python /home/ubuntu/app.py > output.log 2>&1 &
'''


    if stopped_instances:
        to_start = stopped_instances[0]
        print(f"Starting stopped instance: {to_start}")
        ec2.start_instances(InstanceIds=[to_start])
        if wait_for_instance_ok(to_start):
            elbv2.register_targets(TargetGroupArn=target_group_arn, Targets=[{'Id': to_start, 'Port': 80}])
            print(f"Registered {to_start} with target group.")
            cpu_per_instance[to_start] = 0.0
        else:
            print(f"Instance {to_start} not healthy, skipping registration.")
    else:
        print("No stopped instances found. Launching new instance.")
        response = ec2.run_instances(
            ImageId=ami_id,
            InstanceType=instance_type,
            KeyName=key_name,
            MaxCount=1,
            MinCount=1,
            SecurityGroupIds=[security_group_id],
            TagSpecifications=[{
                'ResourceType': 'instance',
                'Tags': [{'Key': 'Purpose', 'Value': 'ScaledInstance'}]
            }],
            UserData=user_data_script
        )
        new_instance_id = response['Instances'][0]['InstanceId']
        print(f"Launched new instance: {new_instance_id}")

        if wait_for_instance_ok(new_instance_id):
            elbv2.register_targets(TargetGroupArn=target_group_arn, Targets=[{'Id': new_instance_id, 'Port': 80}])
            print(f"Registered {new_instance_id} with target group.")
            # Add new instance to CPU metrics dictionary for dashboard (placeholder until real data exists)
            cpu_per_instance[new_instance_id] = 0.0
        else:
            print(f"Instance {new_instance_id} did not become healthy in time. Skipping target registration.")

elif overall_avg_cpu < threshold_low:
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

def update_dashboard(instances_cpu_dict):
    widgets = []
    width = 6
    height = 6
    x, y = 0, 0

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
                    ["AWS/ApplicationELB", "RequestCount", "TargetGroup", "targetgroup/TG1/2b00c3a319ad7c42", "LoadBalancer", "app/ScalerALB/1136bc260d6235a6"]
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
                    ["AWS/ApplicationELB", "Latency", "TargetGroup", "targetgroup/TG1/2b00c3a319ad7c42", "LoadBalancer", "app/ScalerALB/1136bc260d6235a6"]
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

update_dashboard(cpu_per_instance)
