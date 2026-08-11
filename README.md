## Migrate RDS and Aurora logs to CloudWatch Infrequent Access

Automated migration of Amazon RDS and Amazon Aurora CloudWatch log groups from the Standard log class to the Infrequent Access log class using AWS Lambda, Amazon EventBridge, and Amazon SES.  

## Overview                                                                                                                                                                                                                                                                                                                                                          
Amazon RDS and Aurora publish database logs to CloudWatch log groups in the Standard log class by default. Many of these logs — general, error, slow query, audit — shift to periodic use over time: compliance reviews, occasional troubleshooting, historical analysis. The Infrequent Access log class serves these workloads at 50% lower ingestion cost with the same
durability and Logs Insights query support.
  
CloudWatch does not allow a log group's class to be changed after creation, so migrating means deleting and recreating each log group. This solution automates that at fleet scale with tag-based targeting, scheduled re-enablement, and email verification.    

**This solution deletes and recreates CloudWatch log groups. Read this section before deploying.**
- Existing log data is deleted. Log groups are removed as part of the migration. If you need historical logs, export them to Amazon S3 first (see Before you migrate (#before-you-migrate)).
- Temporary log gap. Log delivery is briefly suspended while log groups are deleted and recreated. Run this during a low-activity window.
- No subscription or metric filters. Infrequent Access log groups do not support them. If any monitoring or alerting pipeline depends on metric filters or subscription filters against these log groups, leave those groups in the Standard class — migrating them will silently break those alarms.
- Data protection is not free on IA. Standard log groups include data protection (masking and auditing) at no additional cost. On Infrequent Access it carries an additional per-GB charge. Factor this in if you use it.
                                                                                                                                                                                         
  ## Features                                                                                                                                                                            
                                                                                                                                                                                         
  - Tag-based discovery of RDS instances and Aurora clusters for selective migration                                                                                                     
  - Automated disable, delete, and recreate workflow for CloudWatch log groups                                                                                                           
  - Scheduled re-enablement of log exports via one-time EventBridge rules                                                                                                                
  - Log group class verification after migration                                                                                                                                         
  - Completion notifications via Amazon SES with detailed status per resource                                                                                                            
  - Self-cleanup of EventBridge scheduling rules after execution                                                                                                                         
                                                                                                                                                                                         
  ## Architecture                                                                                                                                                                        
                                                                                                                                                                                         
  The solution uses:                                                                                                                                                                     
                                                                                                                                                                                         
  - **AWS Lambda** (two functions) to orchestrate the migration and re-enablement workflow                                                                                               
  - **Amazon EventBridge** to schedule delayed re-enablement of log exports                                                                                                              
  - **Amazon CloudWatch Logs** for log group recreation in the Infrequent Access class                                                                                                   
  - **Amazon SES** to deliver formatted completion notifications                                                                                                                         
                                                                                                                                                                                         
  ## Prerequisites                                                                                                                                                                       
                                                                                                                                                                                         
  - AWS account with permissions to create Lambda, EventBridge, CloudWatch Logs, and Amazon RDS resources                                                                                
  - An email address verified in Amazon SES to receive notifications                                                                                                                     
  - An Amazon RDS instance or Aurora cluster with at least one CloudWatch log export enabled                                                                                             
  - Python 3.14 runtime support in Lambda                                                                                                                                                
## Before you migrate
  
If you need to keep existing log data, export it to S3 first. Note two constraints: log data can take up to 12 hours to become available for export, and the export captures only what existed when it started — logs generated during the export are not included.
  
For continuous export, consider a near real-time pipeline with Amazon Data Firehose instead. See(https://aws.amazon.com/blogs/database/automate-the-export-of-amazon-rds-for-mysql-or-amazon-aurora-mysql-audit-logs-to-amazon-s3-with-batching-or-near-real-time-processing/).                                                                                                                                                                                 
  ## Lambda Functions                                                                                                                                                                    
                                                                                                                                                                                         
  | Function | File | Description |                                                                                                                                                      
  |----------|------|-------------|                                                                                                                                                      
  | InitiatorCWIA | `InitiatorCWIA.py` | Discovers tagged RDS resources, disables log exports, recreates log groups in Infrequent Access class, and schedules Lambda 2 |                 
  | rds-re-enabler-CWIA | `rds_re_enabler_CWIA.py` | Re-enables log exports, verifies log group class, sends completion email, and cleans up the EventBridge rule |                      
                                                                                                                                                                                         
  ## Configuration                                                                                                                                                                       
                                                                                                                                                                                         
  ### InitiatorCWIA Environment Variables                                                                                                                                                
                                                                                                                                                                                         
  | Variable | Required | Description |                                                                                                                                                  
  |----------|----------|-------------|                                                                                                                                                  
  | `TAG_KEY` | Yes | RDS tag key to identify resources for migration (default: `rds-ia-migration`) |                                                                                    
  | `TAG_VALUE` | Yes | RDS tag value to match (default: `true`) |                                                                                                                       
  | `LOG_CLASS` | Yes | Target log group class (`INFREQUENT_ACCESS` or `STANDARD`) |                                                                                                     
  | `LOG_RETENTION_DAYS` | No | Retention period in days for recreated log groups (e.g., `90`) |                                                                                         
  | `VERIFY_DELAY_MINUTES` | Yes | Delay in minutes before Lambda 2 re-enables logging (default: `2`) |                                                                                  
  | `LAMBDA_ARN` | Yes | ARN of the rds-re-enabler-CWIA Lambda function |                                                                                                                
                                                                                                                                                                                         
  ### rds-re-enabler-CWIA Environment Variables                                                                                                                                          
                                                                                                                                                                                         
  | Variable | Required | Description |                                                                                                                                                  
  |----------|----------|-------------|                                                                                                                                                  
  | `SES_SENDER_EMAIL` | Yes | Verified SES sender email address |                                                                                                                       
  | `SES_RECIPIENT_EMAIL` | Yes | Verified SES recipient email address |                                                                                                                 
                                                                                                                                                                                         
  ## Deployment                                                                                               ### CloudFormation (recommended)
  
  `template.yaml` creates both Lambda functions, their IAM execution roles, and the
  shared policy, with the initiator automatically wired to the re-enabler's ARN.
  Package and upload both functions to S3 first, then deploy the stack. Parameters
  and full commands are in the blog post.
  
  ### Manual
  
  Build each component individually — IAM policy, execution roles, both functions.
  Create the re-enabler **first**; the initiator needs its ARN as an environment
  variable.
  
  Full walkthrough for both paths:
  https://aws.amazon.com/blogs/database/migrate-rds-and-aurora-logs-to-cloudwatch-infrequent-access/

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

