import os
import logging
import sys
import json
from typing import Optional, Dict, Any
from kubernetes import client, config, watch
from google.cloud import dns
from google.api_core import exceptions as gcp_exceptions
from google.auth import default
from google.auth.transport.requests import Request
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Environment variable validation
def validate_env_vars() -> Dict[str, str]:
    """Validate required environment variables."""
    required_vars = {
        'GCP_PROJECT': os.getenv("GCP_PROJECT"),
        'DNS_ZONE_NAME': os.getenv("DNS_ZONE_NAME"),
        'DNS_RECORD_NAME': os.getenv("DNS_RECORD_NAME"),
    }
    
    missing_vars = [var for var, value in required_vars.items() if not value]
    if missing_vars:
        logger.error(f"Missing required environment variables: {missing_vars}")
        sys.exit(1)
    
    # Validate TTL
    ttl_str = os.getenv("TTL", "300")
    try:
        ttl = int(ttl_str)
        if ttl < 1 or ttl > 86400:  # 1 second to 24 hours
            raise ValueError("TTL must be between 1 and 86400 seconds")
    except ValueError as e:
        logger.error(f"Invalid TTL value '{ttl_str}': {e}")
        sys.exit(1)
    
    return {
        'GCP_PROJECT': required_vars['GCP_PROJECT'],
        'DNS_ZONE_NAME': required_vars['DNS_ZONE_NAME'],
        'DNS_RECORD_NAME': required_vars['DNS_RECORD_NAME'],
        'LABEL_SELECTOR': os.getenv("LABEL_SELECTOR", "watch=true"),
        'GEO_LOCATION': os.getenv("GEO_LOCATION", "us"),
        'TTL': ttl
    }

# Global configuration
try:
    CONFIG = validate_env_vars()
    # Initialize credentials for direct API access
    credentials, project = default()
    if not credentials.valid:
        credentials.refresh(Request())
    
    # Keep the original client for zone validation
    dns_client = dns.Client(project=CONFIG['GCP_PROJECT'])
    zone = dns_client.zone(CONFIG['DNS_ZONE_NAME'])
    
    # API endpoint for direct calls
    API_BASE = "https://dns.googleapis.com/dns/v1"
    
except Exception as e:
    logger.error(f"Failed to initialize GCP DNS client: {e}")
    sys.exit(1)

def get_lb_ip(ingress: client.V1Ingress) -> Optional[str]:
    """Extract load balancer IP from ingress status."""
    try:
        if not ingress.status or not ingress.status.load_balancer:
            return None
        
        ingress_status = ingress.status.load_balancer.ingress
        if not ingress_status:
            return None
            
        lb_entry = ingress_status[0]
        return lb_entry.ip or lb_entry.hostname
    except (AttributeError, IndexError) as e:
        logger.warning(f"Failed to extract load balancer IP: {e}")
        return None

def create_or_update_geo_record(ip: str) -> bool:
    """Create or update geo-routed DNS record using direct API, merging with existing geo items."""
    try:
        # Refresh credentials if needed
        if not credentials.valid:
            credentials.refresh(Request())
        
        access_token = credentials.token
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        # Check if record exists first
        list_url = f"{API_BASE}/projects/{CONFIG['GCP_PROJECT']}/managedZones/{CONFIG['DNS_ZONE_NAME']}/rrsets"
        list_response = requests.get(list_url, headers=headers)
        
        existing_record = None
        if list_response.status_code == 200:
            rrsets = list_response.json().get('rrsets', [])
            for rrset in rrsets:
                if rrset.get('name') == CONFIG['DNS_RECORD_NAME'] and rrset.get('type') == 'A':
                    existing_record = rrset
                    break
        
        # Prepare geo items - merge with existing if record exists
        geo_items = []
        current_location_item = {
            "location": CONFIG['GEO_LOCATION'],
            "rrdatas": [ip]
        }
        
        if existing_record and existing_record.get('routingPolicy', {}).get('geo', {}).get('items'):
            # Merge with existing geo items, updating current location or adding if new
            existing_items = existing_record['routingPolicy']['geo']['items']
            
            # Keep all existing items except for current location
            for item in existing_items:
                if item.get('location') != CONFIG['GEO_LOCATION']:
                    geo_items.append(item)
                    
            # Add current location item (updated)
            geo_items.append(current_location_item)
            
            logger.info(f"Merging geo-location '{CONFIG['GEO_LOCATION']}' with {len(existing_items)} existing geo items")
        else:
            # No existing record or no geo routing policy, create new
            geo_items = [current_location_item]
            logger.info(f"Creating new geo-routed record for location '{CONFIG['GEO_LOCATION']}'")
        
        # Prepare the complete record data
        record_data = {
            "name": CONFIG['DNS_RECORD_NAME'],
            "type": "A",
            "ttl": CONFIG['TTL'],
            "routingPolicy": {
                "geo": {
                    "enableFencing": False,
                    "items": geo_items
                }
            }
        }
        
        # Log the geo locations being set
        locations = [item['location'] for item in geo_items]
        logger.info(f"Setting geo-routed DNS record with locations: {', '.join(locations)}")
        
        # Create or update the record
        if existing_record:
            logger.info(f"Updating existing record {CONFIG['DNS_RECORD_NAME']}")
            # For updates, use PATCH method
            update_url = f"{API_BASE}/projects/{CONFIG['GCP_PROJECT']}/managedZones/{CONFIG['DNS_ZONE_NAME']}/rrsets/{CONFIG['DNS_RECORD_NAME']}/A"
            response = requests.patch(update_url, headers=headers, json=record_data)
        else:
            logger.info(f"Creating new geo-routed DNS record for {CONFIG['DNS_RECORD_NAME']}")
            # For creation, use POST method
            create_url = f"{API_BASE}/projects/{CONFIG['GCP_PROJECT']}/managedZones/{CONFIG['DNS_ZONE_NAME']}/rrsets"
            response = requests.post(create_url, headers=headers, json=record_data)
        
        # Handle the response
        if response.status_code in [200, 201]:
            logger.info(f"Successfully {'updated' if existing_record else 'created'} geo-routed DNS record with IP {ip} for location {CONFIG['GEO_LOCATION']}")
            return True
        else:
            logger.error(f"Failed to {'update' if existing_record else 'create'} DNS record. Status: {response.status_code}, Response: {response.text}")
            return False
                
    except Exception as e:
        logger.error(f"Failed to create/update geo-routed DNS record: {e}")
        return False

def setup_kubernetes_client() -> client.NetworkingV1Api:
    """Setup Kubernetes client with proper error handling."""
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes configuration")
    except config.ConfigException:
        try:
            config.load_kube_config()
            logger.info("Loaded local Kubernetes configuration")
        except config.ConfigException as e:
            logger.error(f"Failed to load Kubernetes configuration: {e}")
            sys.exit(1)
    
    return client.NetworkingV1Api()

def watch_ingresses():
    """Watch Kubernetes ingresses for changes with automatic reconnection."""
    v1_api = setup_kubernetes_client()
    
    logger.info(f"Starting to watch ingresses with label selector: {CONFIG['LABEL_SELECTOR']}")
    
    while True:
        w = watch.Watch()
        try:
            logger.info("Starting/Restarting watch stream...")
            for event in w.stream(
                v1_api.list_ingress_for_all_namespaces,
                label_selector=CONFIG['LABEL_SELECTOR'],
                timeout_seconds=300  # 5 minutes timeout
            ):
                try:
                    ingress = event['object']
                    namespace = ingress.metadata.namespace
                    ingress_name = ingress.metadata.name
                    event_type = event['type']
                    
                    logger.info(f"Event: {event_type} Ingress: {namespace}/{ingress_name}")

                    if event_type in ['ADDED', 'MODIFIED']:
                        lb_ip = get_lb_ip(ingress)
                        if lb_ip:
                            logger.info(f"Load Balancer IP detected: {lb_ip}")
                            success = create_or_update_geo_record(lb_ip)
                            if not success:
                                logger.error(f"Failed to update DNS record for {namespace}/{ingress_name}")
                        else:
                            logger.debug(f"No Load Balancer IP available for {namespace}/{ingress_name}")
                    elif event_type == 'DELETED':
                        logger.info(f"Ingress {namespace}/{ingress_name} was deleted")
                        # Note: We don't automatically delete DNS records on ingress deletion
                        # as other ingresses might be using the same record
                        
                except Exception as e:
                    logger.error(f"Error processing ingress event: {e}")
                    continue
                    
        except Exception as e:
            logger.warning(f"Watch stream ended: {e}")
            logger.info("Reconnecting in 5 seconds...")
            import time
            time.sleep(5)
        finally:
            try:
                w.stop()
            except:
                pass

if __name__ == "__main__":
    try:
        watch_ingresses()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
