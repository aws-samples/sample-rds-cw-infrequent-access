## Automate CloudWatch Logs Infrequent Access migration for Amazon RDS and Amazon Aurora

Automated migration of Amazon RDS and Amazon Aurora CloudWatch log groups from the Standard log class to the Infrequent Access log class using AWS Lambda, Amazon EventBridge, and Amazon SES.  

## Overview                                                                                                                                                                            
                                                                                                                                                                                         
  This serverless solution automates the end-to-end migration of CloudWatch log groups for Amazon RDS and Aurora from the Standard class to the Infrequent Access class, reducing log ingestion costs by up to 50%. It uses tag-based resource discovery, handles log group recreation, schedules re-enablement of log exports, and sends verification emails upon completion.                                                                                                                                                                            
                                                                                                                                                                                         
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
                                                                                                                                                                                         
  ## Deployment                                                                                                                                                                          
                                                                                                                                                                                         
  For complete deployment instructions, see the accompanying blog post: [Automate CloudWatch Logs Infrequent Access migration for Amazon RDS and Amazon Aurora]

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

