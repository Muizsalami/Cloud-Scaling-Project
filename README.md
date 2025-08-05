# Cloud-Scaling-Project
AWS Auto-Scaling System
This project implements a lightweight, threshold-based auto-scaling system for AWS EC2 instances using Python and Boto3. It monitors CPU utilization via CloudWatch and dynamically adjusts the number of running instances to match real-time workload demands. The system integrates with an Application Load Balancer (ALB) for traffic distribution and uses Amazon SNS for alerting.

Features
Reactive scaling based on average CPU utilization
Scales out when CPU > 70%, scales in when CPU < 30%
ALB integration for automatic traffic distribution
CloudWatch dashboards for real-time metric visualization
SNS email notifications for scaling events
Designed to run within AWS Free Tier constraints

Components
autoscaler.py: Main Python script triggered by cron (runs every 3 minutes)
Flask app with /health endpoint for simulating CPU-bound workloads
CloudFormation-ready architecture (EC2, ALB, CloudWatch, SNS)

Testing
Traffic was generated using Locust to simulate user load and validate the auto-scaler’s responsiveness under various scenarios.

Requirements
AWS account with IAM role and necessary permissions (EC2, CloudWatch, SNS)
Python 3.x
Boto3
Flask
Cron (for scheduling the script on a local or remote control machine)

Project Status
Completed as part of an MSc dissertation project. Future improvements may include multi-metric scaling, predictive scaling, and containerization.


