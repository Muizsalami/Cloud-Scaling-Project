import boto3
from datetime import datetime, timedelta, timezone
import pytz

# Use UK timezone (London)
uk_tz = pytz.timezone("Europe/London")

# Set the time range (5 minutes ago to now, using UTC)
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(minutes=5)

# Initialize CloudWatch client
cloudwatch = boto3.client('cloudwatch', region_name='eu-north-1')

# Define the metric to fetch
response = cloudwatch.get_metric_statistics(
    Namespace='CWAgent',
    MetricName='mem_used_percent',
    Dimensions=[
        {
            'Name': 'InstanceId',
            'Value': 'i-0851a236955f56eb9'
        },
    ],
    StartTime=start_time,
    EndTime=end_time,
    Period=60,
    Statistics=['Average']
)

# Sort datapoints by Timestamp
datapoints = sorted(response['Datapoints'], key=lambda x: x['Timestamp'])

# Print the data
for point in datapoints:
    timestamp_uk = point['Timestamp'].astimezone(uk_tz)
    print(f"Timestamp (UK): {timestamp_uk.strftime('%Y-%m-%d %H:%M:%S')}, Avg Memory Used: {point['Average']:.2f}%")

# --- Basic scaling decision logic ---
threshold_high = 70  # % memory usage to trigger scale up
threshold_low = 30   # % memory usage to trigger scale down

# Calculate average of all memory data points
if datapoints:
    avg_memory = sum(point['Average'] for point in datapoints) / len(datapoints)
    print(f"\nAverage memory usage over period: {avg_memory:.2f}%")

    if avg_memory > threshold_high:
        print("Decision: SCALE UP – memory usage is high.")
    elif avg_memory < threshold_low:
        print("Decision: SCALE DOWN – memory usage is low.")
    else:
        print("Decision: NO SCALING – memory usage is within acceptable range.")
else:
    print("No datapoints available to make scaling decision.")
