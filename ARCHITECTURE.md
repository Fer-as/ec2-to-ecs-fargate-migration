# Architecture Documentation

> **Historical document (original CoderCo starter).** "Original architecture" below is the
> legacy single-EC2 setup *before* the migration. "Original target architecture" is the
> assignment brief's sketch: it shows HTTPS and Route 53, which this project did **not**
> deploy. The platform as deployed and verified in AWS on 28 October 2026 (HTTP ALB,
> EC2 kept as the rollback target, ECS Fargate at 100 %, shared DynamoDB) is in
> [README.md](README.md#architecture) and [docs/architecture/](docs/architecture/README.md).

## Original Architecture (Legacy EC2, before migration)

### High-Level Overview

```
┌─────────────┐
│   Internet  │
└──────┬──────┘
       │
       │ HTTP (Port 80)
       ▼
┌─────────────────────────────────────────────┐
│         AWS VPC (10.0.0.0/16)               │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │     Public Subnet (10.0.1.0/24)      │  │
│  │                                       │  │
│  │  ┌─────────────────────────────────┐ │  │
│  │  │   EC2 Instance (t3.micro)       │ │  │
│  │  │   Elastic IP: <PUBLIC_IP>       │ │  │
│  │  │                                 │ │  │
│  │  │  ┌──────────────┐              │ │  │
│  │  │  │   Nginx      │              │ │  │
│  │  │  │  Port 80     │              │ │  │
│  │  │  └──────┬───────┘              │ │  │
│  │  │         │                       │ │  │
│  │  │         │ Proxy                 │ │  │
│  │  │         ▼                       │ │  │
│  │  │  ┌──────────────┐              │ │  │
│  │  │  │ Flask App    │              │ │  │
│  │  │  │ (Gunicorn)   │              │ │  │
│  │  │  │ Port 5000    │              │ │  │
│  │  │  └──────────────┘              │ │  │
│  │  │                                 │ │  │
│  │  │  • Systemd Service              │ │  │
│  │  │  • Local Logs                   │ │  │
│  │  │  • In-memory Storage            │ │  │
│  │  └─────────────────────────────────┘ │  │
│  └──────────────────────────────────────┘  │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │    Private Subnet (10.0.2.0/24)     │  │
│  │    (Reserved for future ECS)        │  │
│  └──────────────────────────────────────┘  │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │    Internet Gateway                  │  │
│  └──────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

### Component Details

#### Networking

- **VPC**: 10.0.0.0/16 CIDR block
  - **Public Subnet**: 10.0.1.0/24 (EC2 instance)
  - **Private Subnet**: 10.0.2.0/24 (Reserved for ECS)
  - **Internet Gateway**: Provides internet access
  - **Route Table**: Routes public subnet traffic via IGW

#### Compute

- **EC2 Instance**: Ubuntu 24.04 LTS (official Canonical AMI), Python 3.12
  - **Type**: t3.micro (configurable)
  - **Placement**: Public subnet with Elastic IP
  - **IAM Role**: Allows S3 access for application deployment

#### Application Stack

- **Nginx**: Reverse proxy (Port 80)
  - Serves HTTP traffic
  - Proxies requests to Flask app
  - Handles static content (if any)
  - Access/error logs in `/var/log/nginx/`

- **Flask Application**: Python web framework
  - **WSGI Server**: Gunicorn (4 workers)
  - **Port**: 5000 (localhost only)
  - **Process Manager**: Systemd
  - **Logs**: `/var/log/flask-app/`

#### Storage & Deployment

- **S3 Bucket**: Stores application zip file
  - Versioning enabled
  - Server-side encryption (AES256)
  - Private access only

- **Application Files**: Deployed via user-data script
  1. Downloads zip from S3
  2. Extracts to `/opt/flask-app`
  3. Sets up Python virtual environment
  4. Configures systemd service
  5. Starts Nginx and Flask app

#### Security

- **Security Groups**:
  - Inbound: HTTP (80) from `allowed_cidr_blocks`; SSH (22) only if `ssh_allowed_cidr_blocks` is set (closed by default)
  - Outbound: All traffic allowed

- **Network Security**:
  - EC2 in public subnet (for simplicity)
  - Security group restricts access by CIDR

- **IAM**:
  - EC2 instance role with S3 read permissions
  - Least privilege principle

#### Monitoring (Current Limitations)

- ❌ **No CloudWatch Logs**: Logs only on instance
- ❌ **No Metrics**: Basic EC2 metrics only
- ❌ **No Alarms**: No automated alerts
- ❌ **No Dashboard**: Manual log checking required

### Data Flow

1. **Request Flow**:
   ```
   User → Internet → EC2 Elastic IP → Nginx (Port 80) → Flask App (Port 5000) → Response
   ```

2. **Application Startup**:
   ```
   EC2 Boot → User-Data Script → Download App from S3 → Extract →
   Setup Python Env → Install Dependencies → Configure Systemd →
   Start Gunicorn → Configure Nginx → Start Nginx → Ready
   ```

3. **Deployment Flow**:
   ```
   Terraform Apply → Create S3 Bucket → Upload App Zip →
   Create EC2 Instance → User-Data Executes → App Running
   ```

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/api/v1/products` | List all products |
| GET | `/api/v1/products/{id}` | Get specific product |
| POST | `/api/v1/orders` | Create order |
| GET | `/api/v1/orders` | List all orders |
| GET | `/api/v1/stats` | Application statistics |

### Current Limitations

#### Availability & Scalability
- ❌ Single instance (no redundancy)
- ❌ Single Availability Zone
- ❌ No autoscaling
- ❌ Manual scaling only
- ❌ No load balancing

#### Deployment
- ❌ Manual Terraform deployments
- ❌ No CI/CD pipeline
- ❌ No automated testing
- ❌ No blue/green deployments
- ❌ Downtime during updates

#### Observability
- ❌ Logs only on instance
- ❌ No centralized logging
- ❌ No metrics aggregation
- ❌ No alerting
- ❌ Manual troubleshooting

#### Security
- ❌ Public subnet exposure
- ❌ Limited network segmentation
- ❌ No WAF
- ❌ No DDoS protection

#### Data
- ❌ In-memory storage (data lost on restart)
- ❌ No persistence
- ❌ No backup/recovery

## Original Target Architecture (assignment brief, not as deployed)

The assignment's target sketch (HTTPS and Route 53 were not deployed; see the note at the top):

```
┌─────────────┐
│   Internet  │
└──────┬──────┘
       │
       │ HTTPS (Port 443)
       ▼
┌─────────────────────────────────────────────┐
│      Route53 (DNS)                          │
└──────┬──────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│         AWS VPC (10.0.0.0/16)               │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │     Public Subnet (ALB)              │  │
│  │                                       │  │
│  │  ┌─────────────────────────────────┐ │  │
│  │  │  Application Load Balancer      │ │  │
│  │  │  (Multi-AZ)                     │ │  │
│  │  └──────┬──────────────────────────┘ │  │
│  └─────────┼────────────────────────────┘  │
│            │                                │
│            │ Target Groups                  │
│            ▼                                │
│  ┌──────────────────────────────────────┐  │
│  │   Private Subnet (ECS Tasks)         │  │
│  │                                       │  │
│  │  ┌──────────┐  ┌──────────┐         │  │
│  │  │ Fargate  │  │ Fargate  │         │  │
│  │  │ Task 1   │  │ Task 2   │  ...    │  │
│  │  │          │  │          │         │  │
│  │  │ Container│  │ Container│         │  │
│  │  │ Flask    │  │ Flask    │         │  │
│  │  └──────────┘  └──────────┘         │  │
│  │                                       │  │
│  │  ┌─────────────────────────────────┐ │  │
│  │  │  ECS Service (Auto-scaling)     │ │  │
│  │  └─────────────────────────────────┘ │  │
│  └──────────────────────────────────────┘  │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │  CloudWatch Logs                     │  │
│  │  CloudWatch Metrics                  │  │
│  │  CloudWatch Alarms                   │  │
│  └──────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│  ECR (Container Registry)                   │
│  GitHub Actions (CI/CD)                     │
└─────────────────────────────────────────────┘
```

### Migration Benefits

- ✅ **High Availability**: Multi-AZ deployment
- ✅ **Auto-scaling**: Horizontal scaling based on load
- ✅ **Zero-downtime**: Rolling updates and blue/green
- ✅ **Centralized Logs**: CloudWatch Logs
- ✅ **Metrics & Alarms**: CloudWatch monitoring
- ✅ **Security**: Private subnets, IAM roles per task
- ✅ **CI/CD**: Automated deployments via GitHub Actions
- ✅ **Containerization**: Consistent environments

## Migration Strategy

Students should plan:

1. **Containerization**: Create Dockerfile for Flask app
2. **ECS Infrastructure**: Terraform for ECS cluster, service, ALB
3. **CI/CD Pipeline**: GitHub Actions with OIDC
4. **Observability**: CloudWatch logs, metrics, alarms
5. **Cutover Plan**: Zero-downtime migration strategy

The original migration requirements are in [docs/legacy-starter-readme.md](docs/legacy-starter-readme.md).
