# ExternalDNS GCP Geo routing policies

ExternalDNS-like support for GCP geo-routed DNS policies. This application watches Kubernetes Ingresses and automatically creates or updates geo-routed DNS records in Google Cloud DNS.

## Features

- 🌍 **Geo-routing**: Automatically creates geo-routed DNS records based on ingress load balancer IPs
- 🔄 **Real-time updates**: Watches Kubernetes ingresses for changes and updates DNS records accordingly
- 🌐 **Multi-cluster support**: Intelligent merging of geo-location items across multiple clusters
- 🔌 **Direct REST API**: Uses Cloud DNS REST API for full routing policy support (Python SDK limitations bypassed)
- 🔄 **Auto-reconnection**: Automatic reconnection for watch streams to prevent pod restarts
- 🛡️ **Robust error handling**: Comprehensive error handling with retry logic and proper logging
- 📊 **Production ready**: Includes health checks, structured logging, and security best practices
- 🔒 **Security**: Runs as non-root user with minimal privileges

## Architecture

This application is designed to work across multiple Kubernetes clusters to provide geo-distributed DNS routing:

- **Multi-cluster deployment**: Each cluster runs its own instance with a unique `GEO_LOCATION`
- **Intelligent merging**: When updating DNS records, the application preserves geo-location items from other clusters
- **Direct API usage**: Uses Cloud DNS REST API directly to support routing policies (Python SDK lacks this feature)
- **Automatic recovery**: Watch streams automatically reconnect to prevent service disruption

### Multi-Cluster Flow

1. Each cluster's external-dns-gcp-geo instance watches local ingresses
2. When an ingress gets a load balancer IP, the instance updates the shared DNS record
3. The application merges its geo-location with existing ones from other clusters
4. Result: A single DNS record with multiple geo-routing destinations

## Prerequisites

- Kubernetes cluster with ingress controller
- Google Cloud DNS zone
- GCP service account with DNS admin permissions
- Kubernetes RBAC permissions to read ingresses

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GCP_PROJECT` | Yes | - | Google Cloud Project ID |
| `DNS_ZONE_NAME` | Yes | - | Cloud DNS zone name |
| `DNS_RECORD_NAME` | Yes | - | DNS record name (e.g., "api.example.com.") |
| `GEO_LOCATION` | No | `us` | Geo-location code (e.g., "us", "eu", "asia") |
| `LABEL_SELECTOR` | No | `watch=true` | Label selector for ingresses to watch |
| `TTL` | No | `300` | DNS record TTL in seconds (1-86400) |

### GCP Authentication

The application uses Google Cloud authentication libraries and supports the following authentication methods:

1. **Service Account Key**: Mount service account JSON key and set `GOOGLE_APPLICATION_CREDENTIALS`
2. **Workload Identity**: Use GKE Workload Identity (recommended)
3. **Metadata Service**: Use GCE metadata service if running on GCE

The application automatically handles credential refreshing for long-running operations. When making REST API calls, it:

- Uses the default credential chain to obtain credentials
- Automatically refreshes expired tokens
- Includes proper `Authorization: Bearer <token>` headers for all API calls

## Deployment

### Kubernetes Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: external-dns-gcp-geo
  namespace: dns-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: external-dns-gcp-geo
  template:
    metadata:
      labels:
        app: external-dns-gcp-geo
    spec:
      serviceAccountName: external-dns-gcp-geo
      containers:
      - name: external-dns-gcp-geo
        image: ghcr.io/magma-devs/external-dns-gcp-geo:latest
        env:
        - name: GCP_PROJECT
          value: "your-project-id"
        - name: DNS_ZONE_NAME
          value: "your-zone-name"
        - name: DNS_RECORD_NAME
          value: "api.yourdomain.com."
        - name: GEO_LOCATION
          value: "us"
        - name: LABEL_SELECTOR
          value: "dns.external/geo-route=true"
        resources:
          requests:
            memory: "64Mi"
            cpu: "50m"
          limits:
            memory: "128Mi"
            cpu: "100m"
        securityContext:
          runAsNonRoot: true
          runAsUser: 65534
          readOnlyRootFilesystem: true
          allowPrivilegeEscalation: false
          capabilities:
            drop:
            - ALL
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: external-dns-gcp-geo
  namespace: dns-system
  annotations:
    iam.gke.io/gcp-service-account: external-dns@your-project.iam.gserviceaccount.com
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: external-dns-gcp-geo
rules:
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses"]
  verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: external-dns-gcp-geo
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: external-dns-gcp-geo
subjects:
- kind: ServiceAccount
  name: external-dns-gcp-geo
  namespace: dns-system
```

### GCP Service Account Setup

```bash
# Create service account
gcloud iam service-accounts create external-dns-gcp-geo \
    --display-name="External DNS GCP Geo"

# Grant DNS admin permissions
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:external-dns-gcp-geo@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/dns.admin"

# For Workload Identity
gcloud iam service-accounts add-iam-policy-binding \
    external-dns-gcp-geo@YOUR_PROJECT_ID.iam.gserviceaccount.com \
    --role roles/iam.workloadIdentityUser \
    --member "serviceAccount:YOUR_PROJECT_ID.svc.id.goog[dns-system/external-dns-gcp-geo]"
```

## Usage

### Label your ingresses

Add the label specified in `LABEL_SELECTOR` to ingresses you want to manage:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: api-ingress
  labels:
    dns.external/geo-route: "true"  # This matches the LABEL_SELECTOR
spec:
  ingressClassName: nginx
  rules:
  - host: api.yourdomain.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: api-service
            port:
              number: 80
```

### Multiple regions

For multi-region setups, deploy the application in each region with different `GEO_LOCATION` values:

- Region 1: `GEO_LOCATION=us-central1`
- Region 2: `GEO_LOCATION=europe-west1`
- Region 3: `GEO_LOCATION=asia-southeast1`

**Important**: All instances can start in any order and will intelligently merge their geo-location data. When an instance updates the DNS record, it:

1. Retrieves the current record (if it exists)
2. Preserves all existing geo-location items from other clusters
3. Updates only its own geo-location with the new IP
4. Saves the merged record back to Cloud DNS

This ensures that DNS records remain consistent across all regions, even if clusters are restarted or updated independently.

## Monitoring

### Logs

The application uses structured logging with the following levels:

- `INFO`: Normal operations, ingress events, DNS updates, geo-location merging
- `WARNING`: Recoverable errors, watch stream timeouts, API retries
- `ERROR`: Critical errors, failed DNS updates, authentication failures
- `DEBUG`: Detailed debugging information, missing load balancer IPs

Key log messages to monitor:

- `Successfully created/updated geo-routed DNS record` - DNS update success
- `Merging geo-location 'region' with N existing geo items` - Multi-cluster coordination
- `Starting/Restarting watch stream` - Stream reconnection events
- `Load Balancer IP detected` - Ingress processing

### Health Check

The container includes a health check endpoint accessible at the container level.

### Metrics

Consider adding Prometheus metrics for:
- DNS update success/failure rates
- Ingress processing times
- API call latencies

## Troubleshooting

### Common Issues

1. **Missing environment variables**
   ```
   ERROR - Missing required environment variables: ['GCP_PROJECT']
   ```
   Solution: Ensure all required environment variables are set

2. **GCP authentication failed**
   ```
   ERROR - Failed to initialize GCP DNS client: Could not automatically determine credentials
   ```
   Solution: Check service account setup and Workload Identity configuration

3. **DNS zone not found**
   ```
   ERROR - Failed to initialize GCP DNS client: The requested zone was not found
   ```
   Solution: Verify DNS zone exists and service account has permissions

4. **No load balancer IP**
   ```
   DEBUG - No Load Balancer IP available for default/api-ingress
   ```
   Solution: Wait for ingress controller to assign IP or check ingress configuration

5. **API authentication errors**
   ```
   ERROR - Failed to create/update geo-routed DNS record: 401 Unauthorized
   ```
   Solution: Check GCP credentials and ensure the service account has `roles/dns.admin` permissions

6. **Watch stream timeouts**
   ```
   WARNING - Watch stream ended: TimeoutError
   INFO - Reconnecting in 5 seconds...
   ```
   Solution: This is normal behavior. The application automatically reconnects every 5 minutes or when the stream fails

7. **Geo-location conflicts**
   ```
   INFO - Merging geo-location 'us-central1' with 2 existing geo items
   ```
   Solution: This is expected behavior when multiple clusters update the same DNS record. Each cluster manages its own geo-location.

## Implementation Details

### Why REST API instead of Python SDK?

The Google Cloud DNS Python SDK doesn't support routing policies, which are essential for geo-routing. This application uses the Cloud DNS REST API directly to:

- Create and update DNS records with geo-routing policies
- Support all geo-location codes available in Cloud DNS
- Provide full control over routing policy configuration

### DNS Record Format

The application creates DNS records with the following structure:

```json
{
  "name": "*.example.com.",
  "type": "A", 
  "ttl": 300,
  "routingPolicy": {
    "geo": {
      "enableFencing": false,
      "items": [
        {"location": "us-central1", "rrdatas": ["35.224.7.189"]},
        {"location": "europe-west1", "rrdatas": ["35.224.7.188"]}
      ]
    }
  }
}
```

## Security Considerations

- Application runs as non-root user
- Minimal container privileges
- Read-only root filesystem
- Follows principle of least privilege for GCP permissions
- Uses Workload Identity when possible

## Dependencies

The application uses the following key Python packages:

- `kubernetes>=29.0.0`: For Kubernetes API access and watching ingresses
- `google-cloud-dns>=0.34.0`: For initial DNS zone validation
- `google-auth>=2.0.0`: For GCP authentication and token management
- `requests>=2.25.0`: For direct REST API calls to Cloud DNS

See `requirements.txt` for complete dependency list.

## Related Projects

- [ExternalDNS](https://github.com/kubernetes-sigs/external-dns): The original ExternalDNS project
- [ExternalDNS GCP Geo PR](https://github.com/kubernetes-sigs/external-dns/pull/4928): The upstream PR this project is based on

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.
