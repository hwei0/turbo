#!/usr/bin/env python3
"""Test data generator for the web dashboard.

Creates a ZMQ PUB socket and publishes realistic synthetic diagnostic messages
(bandwidth allocation, service status, network utilization) to test the dashboard
without running the full experiment system. Runs for a configurable duration.
"""

import json
import time
import random
import zmq
import threading
from typing import Dict, Any


class TestDataGenerator:
    """Generates test data for the web dashboard"""

    def __init__(self, zmq_address: str = "tcp://*:5555"):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUB)
        self.socket.bind(zmq_address)
        self.running = False

        # Test configuration
        self.service_ids = [1, 2, 3]
        self.start_time = time.time()

        print(f"Test data generator bound to {zmq_address}")

    def generate_bandwidth_allocation_update(self) -> Dict[str, Any]:
        """Generate a bandwidth allocation update message"""
        current_time = time.time()

        # Generate realistic-looking data
        available_bw = random.uniform(800, 1200)
        rtt = random.uniform(10, 50)

        allocation_map = {}
        total_allocated = 0
        for service_id in self.service_ids:
            allocation = random.uniform(50, 300)
            allocation_map[service_id] = allocation
            total_allocated += allocation

        # Ensure we don't over-allocate
        if total_allocated > available_bw:
            scale_factor = available_bw * 0.8 / total_allocated
            for service_id in allocation_map:
                allocation_map[service_id] *= scale_factor

        expected_utility = random.uniform(0.6, 0.95)
        local_utility = random.uniform(0.3, 0.7)

        return {
            "plot_id": 2,  # BANDWIDTH_ALLOCATION_UPDATE
            "timestamp": current_time,
            "expected_utility": expected_utility,
            "local_utility": local_utility,
            "allocation_map": allocation_map,
            "available_bw": available_bw,
            "rtt": rtt,
        }

    def generate_service_status_update(self, service_id: int) -> Dict[str, Any]:
        """Generate a service status update message"""
        current_time = time.time()

        # Simulate varying success rates
        remote_request_made = random.random() < 0.8  # 80% chance of remote request
        remote_request_successful = remote_request_made and (
            random.random() < 0.85
        )  # 85% success rate

        return {
            "plot_id": 1,  # CLIENT_STATUS_UPDATE
            "service_id": service_id,
            "timestamp": current_time,
            "remote_request_made": remote_request_made,
            "remote_request_successful": remote_request_successful,
        }

    def generate_service_utilization_update(self, service_id: int) -> Dict[str, Any]:
        """Generate a service utilization update message"""
        current_time = time.time()

        max_limit = random.uniform(200, 400)
        snd_rate = random.uniform(50, max_limit * 0.8)
        recv_rate = random.uniform(30, max_limit * 0.6)

        return {
            "plot_id": 3,  # NETWORK_UTILIZATION_UPDATE
            "service_id": service_id,
            "timestamp": current_time,
            "max_limit": max_limit,
            "snd_rate": snd_rate,
            "recv_rate": recv_rate,
        }

    def send_message(self, message: Dict[str, Any]):
        """Send a message via ZMQ"""
        message_json = json.dumps(message)
        self.socket.send_string(message_json)
        print(
            f"Sent: {message['plot_id']} for service {message.get('service_id', 'N/A')}"
        )

    def run_test_loop(self, duration: int = 300):
        """Run the test data generation loop"""
        print(f"Starting test data generation for {duration} seconds...")
        print("Press Ctrl+C to stop early")

        self.running = True
        start_time = time.time()

        try:
            while self.running and (time.time() - start_time) < duration:
                # Send bandwidth allocation update
                bw_update = self.generate_bandwidth_allocation_update()
                self.send_message(bw_update)

                # Send service status updates
                for service_id in self.service_ids:
                    status_update = self.generate_service_status_update(service_id)
                    self.send_message(status_update)

                # Send service utilization updates
                for service_id in self.service_ids:
                    util_update = self.generate_service_utilization_update(service_id)
                    self.send_message(util_update)

                # Wait before next batch
                time.sleep(2)

        except KeyboardInterrupt:
            print("\nStopping test data generation...")

        self.running = False
        print("Test data generation stopped")

    def cleanup(self):
        """Clean up resources"""
        self.socket.close()
        self.context.term()


def main():
    """Main test function"""
    print("=" * 60)
    print("Web Dashboard Test Data Generator")
    print("=" * 60)
    print("This script generates test data for the web dashboard.")
    print("Start the web dashboard in another terminal with:")
    print("  python start_web_dashboard.py")
    print("=" * 60)

    generator = TestDataGenerator()

    try:
        # Give the socket time to bind
        time.sleep(1)

        # Run test loop
        generator.run_test_loop(duration=300)  # Run for 5 minutes

    except Exception as e:
        print(f"Error: {e}")
    finally:
        generator.cleanup()


if __name__ == "__main__":
    main()
