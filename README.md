# Swarmsail

**Swarmsail** manages many Headscale server instances in Docker for self-hosting independent tailnets.

## Quick Start

### Why?
Manage your own Tailscale infrastructure.

### Prerequisites
- Docker & Docker Compose
- Python 3.6+

### Notes

It is not possible to use Cloudflare as a reverse proxy for Headscale, so swarmsail should be setup in a way that allows direct communication from the internet.

### Setup

1. **Clone and navigate to the project:**
   ```bash
   cd swarmsail
   ```

2. **Define your Headscale instances** in `instances.json`:
   ```json
   {
     "headscale": {
       "instances": [
         {
           "id": 1,
           "web_port": 8081,
           "stun_port": 3478,
           "stun_port_udp": true
         },
         {
           "id": 2,
           "web_port": 8082,
           "stun_port": 3479,
           "stun_port_udp": true
         }
       ]
     },
     ...
   }
   ```

3. **Generate docker-compose.yml:**
   ```bash
   python3 manage_headscale.py generate
   ```

4. **Start the services:**
   ```bash
   docker-compose up -d
   ```

## Managing Instances

### Add a New Instance

Edit `instances.json` and add a new entry under `headscale.instances`:

```json
{
  "id": 3,
  "web_port": 8083,
  "stun_port": 3480,
  "stun_port_udp": true
}
```

Then regenerate:
```bash
python3 manage_headscale.py generate
docker-compose up -d
```

### Remove an Instance

1. Delete the instance entry from `instances.json`
2. Regenerate and restart:
   ```bash
   python3 manage_headscale.py generate
   docker-compose down
   docker-compose up -d
   ```

### Scale to Hundreds

Simply add instances to `instances.json`. The script handles generating the entire compose file. Example for 100 instances:

```python
# Generate instances programmatically
import json

config = json.load(open('instances.json'))
instances = config['headscale']['instances']

# Add instances 3-100
for i in range(3, 101):
    instances.append({
        "id": i,
        "web_port": 8080 + i,
        "stun_port": 3477 + i,
        "stun_port_udp": True
    })

json.dump(config, open('instances.json', 'w'), indent=2)
```

Then run: `python3 manage_headscale.py generate && docker-compose up -d`

## Configuration

### instances.json Structure

```json
{
  "headscale": {
    "instances": [
      {
        "id": 1,
        "web_port": 8081,
        "stun_port": 3478,
        "stun_port_udp": true
      }
    ]
  },
  "npm": {
    "enabled": true,
    "container_name": "npmplus",
    "image": "docker.io/zoeyvid/npmplus:latest",
    "admin_ui_port": 81,
    "http_port": 80,
    "https_port": 443,
    "timezone": "America/New_York",
    "acme_email": "your-email@example.com"
  },
  "crowdsec": {
    "enabled": false,
    "timezone": "America/New_York"
  },
  "network": {
    "name": "npm-net",
    "driver": "bridge"
  }
}
```

### Key Settings

**Headscale Instances:**
- `id`: Unique instance number (auto-increments volumes and container names)
- `web_port`: Port for web UI (maps to container port 8080)
- `stun_port`: STUN server port (optional, typically 3478+)
- `stun_port_udp`: Enable UDP for STUN

**NPM Plus (Nginx Proxy Manager):**
- Set `enabled: true` to include reverse proxy
- Configure `acme_email` for Let's Encrypt SSL certificates
- Adjust timezone as needed

**CrowdSec Security:**
- Set `enabled: true` to add security monitoring
- Complements NPM Plus with threat detection

## Important Notes

1. **DNS Configuration**: DNS records for your tailnets must point directly to the hosting server (no Cloudflare/proxy services that block UDP)
2. **Persistent Data**: Each instance stores config and data in `./headscale/{id}/config` and `./headscale/{id}/data`
3. **Port Management**: Each instance needs unique ports; auto-increment by ID
4. **Network**: All services run on the `npm-net` bridge network
5. **Data Backup**: Back up `./headscale/*/data` directories for persistence

## Project Structure

```
.
├── instances.json              # Instance definitions (edit this!)
├── manage_headscale.py         # Generator script
├── docker-compose.yml          # Generated (don't edit directly)
├── headscale/
│   ├── 1/
│   │   ├── config/            # Headscale instance 1 config
│   │   └── data/              # Headscale instance 1 data
│   ├── 2/
│   │   ├── config/
│   │   └── data/
│   └── ...
├── npm/
│   └── data/                  # NPM Plus data (SSL certs, configs)
└── crowdsec/
    ├── conf/                  # CrowdSec config
    └── data/                  # CrowdSec data
```

## Troubleshooting

**Port conflicts?** Adjust port numbers in `instances.json`

**Container won't start?** Check Docker logs:
```bash
docker-compose logs headscale-1
```

**Need to regenerate after changes?** Always run:
```bash
python3 manage_headscale.py generate
```
