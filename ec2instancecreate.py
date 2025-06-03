import boto3

ec2 = boto3.client('ec2', region_name='eu-north-1')

response = ec2.run_instances(
    ImageId='ami-0c1ac8a41498c1a9c',
    InstanceType='t3.micro',
    KeyName='salamikey',
    MinCount=1,
    MaxCount=1,
    SecurityGroupIds=['sg-0457955da20e50a9e'],
    TagSpecifications=[
        {
            'ResourceType': 'instance',
            'Tags': [{'Key': 'Purpose', 'Value': 'ManualTest'}]
        }
    ]
)

instance_id = response['Instances'][0]['InstanceId']
print(f"Launched instance: {instance_id}")
