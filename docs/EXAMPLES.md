# Wie by Aftergraph — Examples

## Quick Examples

### 1. Ingest an Observation

```bash
curl -X POST http://localhost:8087/v1/observations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{
    "tenant_id": "acme-corp",
    "source": "email",
    "text": "Køb nye kontormøbler til kontoret",
    "external_id": "msg-12345",
    "actor": "john.doe@acme.com",
    "metadata": {
      "priority": "high",
      "department": "operations"
    }
  }'
```

**Response:**

```json
{
  "action": "created",
  "work_item_id": "wi_abc123",
  "candidate_count": 2
}
```

---

### 2. List Work Items

```bash
curl http://localhost:8087/v1/work-items?tenant_id=acme-corp \
  -H "Authorization: Bearer your-token"
```

**Response:**

```json
{
  "count": 2,
  "work_items": [
    {
      "id": "wi_abc123",
      "tenant_id": "acme-corp",
      "title": "Køb kontormøbler",
      "status": "candidate",
      "canonical_key": "abc123def456",
      "created_at": "2026-09-05T10:30:00Z",
      "updated_at": "2026-09-05T10:30:00Z"
    }
  ]
}
```

---

### 3. Approve a Work Item

```bash
curl -X POST http://localhost:8087/v1/work-items/wi_abc123/review?tenant_id=acme-corp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{
    "action": "approve",
    "actor": "manager@acme.com",
    "reason": "Approved for Q4 budget"
  }'
```

**Response:**

```json
{
  "id": "wi_abc123",
  "status": "approved",
  "updated_at": "2026-09-05T11:00:00Z"
}
```

---

### 4. Publish to RenOS

```bash
curl -X POST http://localhost:8087/v1/work-items/wi_abc123/publish?tenant_id=acme-corp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{
    "destination": "renos"
  }'
```

**Response:**

```json
{
  "id": "pub_abc123",
  "work_item_id": "wi_abc123",
  "destination": "renos",
  "external_id": "job-456",
  "response": {
    "status": "queued"
  },
  "published_at": "2026-09-05T11:10:00Z"
}
```

---

### 5. Get Evidence Envelope

```bash
curl http://localhost:8087/v1/work-items/wi_abc123/evidence?tenant_id=acme-corp \
  -H "Authorization: Bearer your-token"
```

**Response:**

```json
{
  "payload": {
    "tenant_id": "acme-corp",
    "work_item_id": "wi_abc123",
    "title": "Køb kontormøbler",
    "canonical_key": "abc123def456",
    "observations": [...]
  },
  "hmac": "sha256:abc123...",
  "algorithm": "HMAC-SHA256",
  "created_at": "2026-09-05T11:15:00Z"
}
```

---

### 6. Check Monitoring

```bash
curl http://localhost:8087/v1/monitoring \
  -H "Authorization: Bearer your-token"
```

**Response:**

```json
{
  "system": {
    "cpu_percent": 12.5,
    "memory_percent": 45.2,
    "memory_used_gb": 3.6,
    "memory_total_gb": 8.0,
    "disk_percent": 65.0,
    "disk_used_gb": 52.0,
    "disk_total_gb": 80.0
  },
  "service": {
    "total_ingested": 150,
    "total_candidates": 45,
    "total_approved": 12,
    "total_published": 8
  },
  "timestamp": "2026-09-05T11:20:00Z"
}
```

---

## Python Client Examples

### Basic Usage

```python
import httpx

# Configure client
client = httpx.Client(
    base_url="http://localhost:8087",
    headers={"Authorization": "Bearer your-token"}
)

# Ingest observation
response = client.post("/v1/observations", json={
    "tenant_id": "acme-corp",
    "source": "email",
    "text": "Køb nye kontormøbler til kontoret",
    "external_id": "msg-12345",
    "actor": "john.doe@acme.com"
})
print(response.json())

# List work items
response = client.get("/v1/work-items", params={"tenant_id": "acme-corp"})
print(response.json())

# Approve work item
response = client.post(
    "/v1/work-items/wi_abc123/review",
    params={"tenant_id": "acme-corp"},
    json={
        "action": "approve",
        "actor": "manager@acme.com",
        "reason": "Approved for Q4 budget"
    }
)
print(response.json())
```

### Async Usage

```python
import httpx
import asyncio

async def main():
    async with httpx.AsyncClient(
        base_url="http://localhost:8087",
        headers={"Authorization": "Bearer your-token"}
    ) as client:
        # Ingest observation
        response = await client.post("/v1/observations", json={
            "tenant_id": "acme-corp",
            "source": "email",
            "text": "Køb nye kontormøbler til kontoret"
        })
        print(response.json())
        
        # List work items
        response = await client.get("/v1/work-items", params={"tenant_id": "acme-corp"})
        print(response.json())

asyncio.run(main())
```

---

## Integration Examples

### RenOS Integration

```python
from aftergraph_work_intelligence.api import create_app
from aftergraph_work_intelligence.publishers import RenosPublisher
from aftergraph_work_intelligence.policy import PolicyStore, TenantPolicy

# Configure policy
policy_store = PolicyStore()
policy_store.put("renos", TenantPolicy(
    allowed_sources={"conversation", "email", "calendar", "renos"},
    allowed_destinations={"renos", "works"},
    allow_works=True,
    max_work_items=100,
    max_priority="high",
))

# Configure publisher
renos_publisher = RenosPublisher(
    base_url="http://renos:3000",
    company_id="company-123"
)

# Create app
app = create_app(
    db_path="./data/work-intelligence.db",
    publisher=renos_publisher,
    policy_store=policy_store,
)
```

### WORKS Integration

```python
from aftergraph_work_intelligence.publishers import WorksPublisher

works_publisher = WorksPublisher(
    base_url="http://works:8080"
)

# Configure app with WORKS publisher
app = create_app(
    db_path="./data/work-intelligence.db",
    publisher=works_publisher,
)
```

---

## Docker Examples

### Run with Docker Compose

```bash
# Start all services
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f work-intelligence

# Stop all
docker-compose down
```

### Run Standalone Container

```bash
# Build image
docker build -t aftergraph-work-intelligence .

# Run container
docker run -d \
  -p 8087:8087 \
  -e AFTERGRAPH_API_TOKEN=my-secret-token \
  -e AFTERGRAPH_RATE_LIMIT=120 \
  -v work-intelligence-data:/app/data \
  --name work-intelligence \
  aftergraph-work-intelligence

# Check logs
docker logs -f work-intelligence

# Stop container
docker stop work-intelligence
```

---

## Monitoring Examples

### Health Check

```bash
# Simple health check
curl http://localhost:8087/healthz

# With jq for pretty output
curl -s http://localhost:8087/healthz | jq .
```

### System Metrics

```bash
# Get system metrics
curl -H "Authorization: Bearer your-token" \
  http://localhost:8087/v1/monitoring | jq .

# Monitor CPU usage
watch -n 5 'curl -s -H "Authorization: Bearer your-token" \
  http://localhost:8087/v1/monitoring | jq ".system.cpu_percent"'
```

### Service Metrics

```bash
# Get service metrics
curl -H "Authorization: Bearer your-token" \
  http://localhost:8087/v1/metrics | jq .

# Track ingestion rate
watch -n 10 'curl -s -H "Authorization: Bearer your-token" \
  http://localhost:8087/v1/metrics | jq ".total_ingested"'
```

---

## Troubleshooting Examples

### Debug Mode

```bash
# Enable debug logging
export AFTERGRAPH_LOG_LEVEL=DEBUG
python -m aftergraph_work_intelligence.api

# View debug logs
tail -f /var/log/work-intelligence/debug.log
```

### Rate Limiting

```bash
# Check rate limit status
curl -v http://localhost:8087/v1/version 2>&1 | grep -i "rate"

# Increase rate limit
export AFTERGRAPH_RATE_LIMIT=120
```

### Authentication Issues

```bash
# Test with curl verbose
curl -v -H "Authorization: Bearer your-token" \
  http://localhost:8087/v1/version

# Check token
echo $AFTERGRAPH_API_TOKEN
```
