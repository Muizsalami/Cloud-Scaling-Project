import boto3
from datetime import datetime, timedelta, timezone
import pytz

# Configurable Parameters
INSTANCE_ID = 'i-0851a236955f56eb9'
AMI_ID = 'ami-0c1ac8a41498c1a9c'  # Your AMI ID
INSTANCE_TYPE = 't3.micro'        # Ensure it's free-tier eligible
KEY_NAME = 'salamikey'
SECURITY_GROUP_IDS = ['sg-0457955da20e50a9e']
SUBNET_ID = 'subnet-043f03c80e0054b2b'
REGION = 'eu-north-1'

# Scaling Thresholds
threshold_high = 70
threshold_low = 30

# Timezones
uk_tz = pytz.timezone("Europe/London")
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(minutes=5)

# Clients
cloudwatch = boto3.client('cloudwatch', region_name=REGION)
ec2 = boto3.client('ec2', region_name=REGION)

# Get memory usage data
response = cloudwatch.get_metric_statistics(
    Namespace='CWAgent',
    MetricName='mem_used_percent',
    Dimensions=[{'Name': 'InstanceId', 'Value': 'i-0851a236955f56eb9'}],
    StartTime=start_time,
    EndTime=end_time,
    Period=60,
    Statistics=['Average']
)

datapoints = sorted(response['Datapoints'], key=lambda x: x['Timestamp'])

# Display results with UK time
for point in datapoints:
    timestamp_uk = point['Timestamp'].astimezone(uk_tz)
    print(f"Timestamp (UK): {timestamp_uk.strftime('%Y-%m-%d %H:%M:%S')}, Avg Memory Used: {point['Average']:.2f}%")

# Scaling Decision
if datapoints:
    avg_memory = sum(p['Average'] for p in datapoints) / len(datapoints)
    print(f"\nAverage memory usage over period: {avg_memory:.2f}%")

    if avg_memory > threshold_high:
        print("Decision: SCALE UP – memory usage is high.")
        print("Launching a new EC2 instance...")

        try:
            ec2.run_instances(
                ImageId=AMI_ID,
                InstanceType=INSTANCE_TYPE,
                KeyName=KEY_NAME,
                MaxCount=1,
                MinCount=1,
                SecurityGroupIds=SECURITY_GROUP_IDS,
                SubnetId=SUBNET_ID,
                TagSpecifications=[
                    {
                        'ResourceType': 'instance',
                        'Tags': [{'Key': 'Name', 'Value': 'AutoScaledInstance'}]
                    }
                ]
            )
            print("✅ New instance launched successfully.")
        except Exception as e:
            print("❌ Error launching instance:", e)

    elif avg_memory < threshold_low:
        print("Decision: SCALE DOWN – memory usage is low. (Scaling down logic not yet implemented.)")
    else:
        print("Decision: NO SCALING – memory usage is within acceptable range.")
else:
    print("No datapoints available to make scaling decision.")
