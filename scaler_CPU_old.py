import boto3
from datetime import datetime, timedelta, timezone
import pytz
import time
import json
import logging

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- Configuration ---
region = 'eu-north-1'
sns_topic_arn = 'arn:aws:sns:eu-north-1:135699253595:ScalingAlerts'
load_balancer_name = 'app/ScalerALB/1136bc260d6235a6'

ami_id = 'ami-0c1ac8a41498c1a9c'
instance_type = 't3.micro'
security_group_id = 'sg-0457955da20e50a9e'
key_name = 'salamikey'
target_group_arn = 'arn:aws:elasticloadbalancing:eu-north-1:135699253595:targetgroup/TG1/2b00c3a319ad7c42'

primary_tag_key = 'Role'
primary_tag_value = 'Primary'

uk_tz = pytz.timezone("Europe/London")

cloudwatch = boto3.client('cloudwatch', region_name=region)
ec2 = boto3.client('ec2', region_name=region)
sns = boto3.client('sns', region_name=region)
elbv2 = boto3.client('elbv2', region_name=region)

# --- Functions ---

def send_alert(subject, message):
    sns.publish(TopicArn=sns_topic_arn, Subject=subject, Message=message)

def wait_for_instance_ok(instance_id, timeout=300, interval=15):
    """Wait for instance status to be 'ok' and running"""
    logger.info(f"Waiting for instance {instance_id} to be running and status OK...")
    waiter = ec2.get_waiter('instance_running')
    try:
        waiter.wait(InstanceIds=[instance_id], WaiterConfig={'Delay': interval, 'MaxAttempts': int(timeout/interval)})
    except Exception as e:
        logger.error(f"Error waiting for instance to run: {e}")
        return False

    # Now wait for system status checks to pass
    for _ in range(int(timeout/interval)):
        statuses = ec2.describe_instance_status(InstanceIds=[instance_id])
        if statuses['InstanceStatuses']:
            status = statuses['InstanceStatuses'][0]
            sys_status = status['SystemStatus']['Status']
            inst_status = status['InstanceStatus']['Status']
            if sys_status == 'ok' and inst_status == 'ok':
                logger.info(f"Instance {instance_id} is running and status OK.")
                return True
        time.sleep(interval)

    logger.warning(f"Timeout waiting for instance {instance_id} status OK.")
    return False

def wait_for_target_healthy(target_group_arn, instance_id, timeout=300, interval=15):
    """Wait for an instance to show as healthy in the target group"""
    logger.info(f"Waiting for instance {instance_id} to become healthy in target group...")
    elapsed = 0
    while elapsed < timeout:
        response = elbv2.describe_target_health(TargetGroupArn=target_group_arn)
        for target_desc in response['TargetHealthDescriptions']:
            target = target_desc['Target']
            state = target_desc['TargetHealth']['State']
            if target['Id'] == instance_id and state == 'healthy':
                logger.info(f"Instance {instance_id} is healthy in target group.")
                return True
        time.sleep(interval)
        elapsed += interval
    logger.warning(f"Timeout waiting for instance {instance_id} to become healthy.")
    return False

def get_running_instances():
    response = ec2.describe_instances(Filters=[{'Name': 'instance-state-name', 'Values': ['running']}])
    all_running_instances = []
    primary_instances = []

    for reservation in response['Reservations']:
        for instance in reservation['Instances']:
            instance_id = instance['InstanceId']
            all_running_instances.append(instance_id)
            tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
            if tags.get(primary_tag_key) == primary_tag_value:
                primary_instances.append(instance_id)
    return all_running_instances, primary_instances

def get_cpu_utilization(instance_id, start_time, end_time):
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
        logger.info(f"Instance {instance_id} - Timestamp (UK): {timestamp_uk.strftime('%Y-%m-%d %H:%M:%S')}, Avg CPU: {avg_cpu:.2f}%")
        return avg_cpu
    else:
        logger.info(f"No CPU data for instance {instance_id}. Assuming 0%.")
        return 0.0

def scale_up(cpu_average):
    logger.info("SCALE UP triggered. Checking for stopped instances.")
    send_alert("SCALE UP Triggered", f"High CPU ({cpu_average:.2f}%).")

    stopped_response = ec2.describe_instances(Filters=[{'Name': 'instance-state-name', 'Values': ['stopped']}])
    stopped_instances = [i['InstanceId'] for r in stopped_response['Reservations'] for i in r['Instances']]

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
        logger.info(f"Starting stopped instance: {to_start}")
        ec2.start_instances(InstanceIds=[to_start])
        if wait_for_instance_ok(to_start):
            # Register immediately after running
            elbv2.register_targets(TargetGroupArn=target_group_arn, Targets=[{'Id': to_start, 'Port': 80}])
            logger.info(f"Registered {to_start} with target group.")
            # Then wait for target to become healthy
            if wait_for_target_healthy(target_group_arn, to_start):
                logger.info(f"Instance {to_start} is healthy and ready.")
            else:
                logger.warning(f"Instance {to_start} failed to become healthy in target group.")
            return to_start
        else:
            logger.warning(f"Instance {to_start} not healthy, skipping registration.")
            return None
    else:
        logger.info("No stopped instances found. Launching new instance.")
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
        logger.info(f"Launched new instance: {new_instance_id}")

        if wait_for_instance_ok(new_instance_id):
            # Register before waiting for health
            elbv2.register_targets(TargetGroupArn=target_group_arn, Targets=[{'Id': new_instance_id, 'Port': 80}])
            logger.info(f"Registered {new_instance_id} with target group.")
            if wait_for_target_healthy(target_group_arn, new_instance_id):
                logger.info(f"Instance {new_instance_id} is healthy and ready.")
            else:
                logger.warning(f"Instance {new_instance_id} failed to become healthy in target group.")
            return new_instance_id
        else:
            logger.warning(f"Instance {new_instance_id} did not become healthy in time. Skipping target registration.")
            return None

def scale_down(cpu_average, all_running_instances, primary_instances):
    candidates_to_stop = [i for i in all_running_instances if i not in primary_instances]
    if candidates_to_stop:
        instance_to_stop = candidates_to_stop[-1]
        logger.info(f"SCALE DOWN: Stopping {instance_to_stop}")
        elbv2.deregister_targets(TargetGroupArn=target_group_arn, Targets=[{'Id': instance_to_stop, 'Port': 80}])
        ec2.stop_instances(InstanceIds=[instance_to_stop])
        send_alert("SCALE DOWN Triggered", f"Low CPU ({cpu_average:.2f}%). Stopped {instance_to_stop}.")
    else:
        logger.info("No non-primary instances to stop. Skipping.")
        send_alert("SCALE DOWN Skipped", "Only primary instances are running.")

def get_healthy_instance_ids(target_group_arn):
    response = elbv2.describe_target_health(TargetGroupArn=target_group_arn)
    healthy_ids = [
        target['Target']['Id']
        for target in response['TargetHealthDescriptions']
        if target['TargetHealth']['State'] == 'healthy'
    ]
    return healthy_ids

def update_dashboard(instance_ids):
    widgets = []
    width = 6
    height = 6
    x, y = 0, 0

    for instance_id in instance_ids:
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
                "yAxis": {"left": {"min": 0, "max": 100}}
            }
        }
        widgets.append(widget)
        x += width
        if x >= 24:
            x = 0
            y += height

    # Overall CPU widget
    avg_widget = {
        "type": "metric",
        "x": 0,
        "y": y + height,
        "width": 24,
        "height": height,
        "properties": {
            "metrics": [
                ["AWS/EC2", "CPUUtilization", "InstanceId", instance_id]
                for instance_id in instance_ids
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

    # ELB Request Count widget
    elb_widget = {
        "type": "metric",
        "x": 0,
        "y": y + height * 2,
        "width": 12,
        "height": 6,
        "properties": {
            "title": "ELB Request Count",
            "region": region,
            "metrics": [
                ["AWS/ApplicationELB", "RequestCount", "TargetGroup", target_group_arn.split(":")[-1], "LoadBalancer", load_balancer_name]
            ],
            "stat": "Sum",
            "period": 60,
            "view": "timeSeries",
            "yAxis": {"left": {"min": 0}}
        }
    }
    widgets.append(elb_widget)

    dashboard_body = {
        "widgets": widgets
    }

    cloudwatch.put_dashboard(
        DashboardName='AutoScalingMonitoring',
        DashboardBody=json.dumps(dashboard_body)
    )
    logger.info(f"✅ CloudWatch dashboard updated for instances: {instance_ids}")

# --- Main run logic ---
def main():
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=5)

    all_running_instances, primary_instances = get_running_instances()

    if not all_running_instances:
        logger.warning("No running instances detected. Exiting.")
        return

    logger.info(f"All running instances: {all_running_instances}")
    logger.info(f"Primary (protected) instances: {primary_instances}")

    cpu_per_instance = {}
    for instance_id in all_running_instances:
        cpu_per_instance[instance_id] = get_cpu_utilization(instance_id, start_time, end_time)

    overall_avg_cpu = sum(cpu_per_instance.values()) / len(cpu_per_instance)
    logger.info(f"Overall average CPU utilization: {overall_avg_cpu:.2f}%")

    threshold_high = 60
    threshold_low = 20

    if overall_avg_cpu > threshold_high:
        new_instance_id = scale_up(overall_avg_cpu)
        if new_instance_id:
            cpu_per_instance[new_instance_id] = 0.0
    elif overall_avg_cpu < threshold_low:
        scale_down(overall_avg_cpu, all_running_instances, primary_instances)
    else:
        logger.info("NO SCALING – CPU usage within acceptable range.")

    healthy_instance_ids = get_healthy_instance_ids(target_group_arn)

    if healthy_instance_ids:
        update_dashboard(healthy_instance_ids)
    else:
        logger.warning("⚠️ No healthy instances found in target group.")

if __name__ == "__main__":
    main()
