import os
import logging
import sys
from typing import Optional, Dict, Any
from kubernetes import client, config, watch
from google.cloud import dns
from google.api_core import exceptions as gcp_exceptions

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
    dns_client = dns.Client(project=CONFIG['GCP_PROJECT'])
    zone = dns_client.zone(CONFIG['DNS_ZONE_NAME'])
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
    """Create or update geo-routed DNS record."""
    try:
        # Create the new geo-routed record
        geo_set = zone.resource_record_set(
            CONFIG['DNS_RECORD_NAME'], 
            'A', 
            CONFIG['TTL'], 
            [ip]
        )
        geo_set.routing_policy = {
            "geo": {
                CONFIG['GEO_LOCATION']: [ip]
            }
        }

        # Check if record exists by filtering instead of listing all records
        existing_record = None
        try:
            # Try to get the specific record
            for record in zone.list_resource_record_sets(name=CONFIG['DNS_RECORD_NAME']):
                if record.name == CONFIG['DNS_RECORD_NAME'] and record.record_type == 'A':
                    existing_record = record
                    break
        except Exception as e:
            logger.warning(f"Failed to check existing records: {e}")

        # Create the change set
        changes = zone.changes()
        
        if existing_record:
            logger.info(f"Updating existing record {existing_record.name}")
            changes.delete_record_set(existing_record)
        else:
            logger.info(f"Creating new geo-routed DNS record for {CONFIG['DNS_RECORD_NAME']}")
        
        changes.add_record_set(geo_set)
        
        # Apply changes with retry logic
        max_retries = 3
        for attempt in range(max_retries):
            try:
                changes.create()
                logger.info(f"Successfully {'updated' if existing_record else 'created'} geo-routed DNS record to IP {ip}")
                return True
            except gcp_exceptions.GoogleAPIError as e:
                if attempt == max_retries - 1:
                    logger.error(f"Failed to update DNS record after {max_retries} attempts: {e}")
                    return False
                logger.warning(f"DNS update attempt {attempt + 1} failed, retrying: {e}")
                
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
    """Watch Kubernetes ingresses for changes."""
    v1_api = setup_kubernetes_client()
    w = watch.Watch()
    
    logger.info(f"Starting to watch ingresses with label selector: {CONFIG['LABEL_SELECTOR']}")
    
    try:
        for event in w.stream(
            v1_api.list_ingress_for_all_namespaces,
            label_selector=CONFIG['LABEL_SELECTOR']
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
        logger.error(f"Error watching ingresses: {e}")
        sys.exit(1)
    finally:
        w.stop()

if __name__ == "__main__":
    try:
        watch_ingresses()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
