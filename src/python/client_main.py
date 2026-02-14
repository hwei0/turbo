"""Client-side process orchestrator for the AV bandwidth sharing experiment.

Reads a YAML configuration file and launches all client-side components as separate
processes in a multiprocessing Pool:
  - CameraDataStream processes (one per USB camera / perception service)
  - Client processes (one per service, handling preprocessing and QUIC send/receive)
  - BandwidthAllocator process (solves LP-based bandwidth allocation)
  - PingHandler process (measures RTT to the remote server)

Shutdown mechanism:
  Uses ZMQ PUB/SUB kill-switch sockets rather than relying on SIGINT propagation.
  Each child process ignores SIGINT (via signal.SIG_IGN set before __init__) and
  instead polls a ZMQ SUB socket for an "ABORT" message from this orchestrator.

  This cooperative shutdown approach is necessary because:
  1. ZMQ REQ/REP sockets have a strict send-recv-send-recv state machine. A SIGINT
     arriving mid-handshake leaves the socket in an invalid state, making graceful
     cleanup impossible (the process cannot send or recv on the broken socket).
  2. SIGINT can interrupt blocking operations at arbitrary points -- during shared
     memory writes, pickle serialization, or Parquet file flushes -- leading to
     corrupted data or resource leaks.
  3. Kill-switch polling happens at well-defined points in each process's main loop,
     ensuring shutdown occurs at clean boundaries where all in-flight operations have
     completed and resources can be released deterministically.

  On SIGINT, this orchestrator broadcasts "ABORT" on all kill-switch PUB sockets,
  then waits for child processes to complete. Each child detects the signal on its
  next poll iteration, flushes logged data to Parquet, closes ZMQ sockets and SHM
  regions, and exits cleanly.
"""

from datetime import datetime
import logging
import logging.config
from logging import FileHandler
from pathlib import Path
import signal
import traceback
import yaml
from multiprocessing import Pool, Queue, Pipe
import time
import os
import argparse

import zmq

from bandwidth_allocator import BandwidthAllocator, BandwidthAllocatorConfig
from camera_stream.camera_data_stream import CameraDataStream, CameraDataStreamConfig
from client import Client, ClientConfig
from ping_handler.ping_handler import PingHandler, PingHandlerConfig
from util.plotting_main import MainPlotter, MainPlotterConfig
from exceptions import GracefulShutdown

logger = logging.getLogger("client_main")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config_path",
        type=str,
        required=True,
        help="path to server config file",
    )

    args = parser.parse_args()

    config_doc = None
    with open(args.config_path) as stream:
        config_doc = yaml.safe_load(stream)

    if config_doc["logging_config_filepath"] is not None:
        log_config = None
        with open(config_doc["logging_config_filepath"]) as stream:
            log_config = yaml.safe_load(stream)

        logging.config.dictConfig(log_config)

    # Create timestamped run directory and ZMQ directory for this experiment run
    experiment_output_dir = Path(config_doc["experiment_output_dir"])
    client_subdir = config_doc.get("client_subdir", "client")
    zmq_dir = Path(config_doc["zmq_dir"])

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = experiment_output_dir / f"client_main_{timestamp}"
    client_dir = run_dir / client_subdir

    client_dir.mkdir(parents=True, exist_ok=True)
    zmq_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Experiment run directory: %s", run_dir)

    def resolve_zmq(name: str) -> str:
        return f"ipc://{zmq_dir / name}"

    # Resolve paths in config dicts before creating Pydantic models
    for doc in config_doc["main_client_config_list"]:
        doc["client_savedir"] = str(client_dir)
        for key in [
            "camera_bidirectional_zmq_sockname",
            "bandwidth_allocation_incoming_zmq_sockname",
            "quic_rcv_zmq_sockname",
            "quic_snd_zmq_sockname",
            "outgoing_zmq_diagnostic_sockname",
            "zmq_kill_switch_sockname",
        ]:
            doc[key] = resolve_zmq(doc[key])

    bw_doc = config_doc["bandwidth_allocator_config"]
    for key in [
        "outgoing_zmq_diagnostic_sockname",
        "bidirectional_zmq_quic_sockname",
        "zmq_kill_switch_sockname",
        "bidirectional_zmq_ping_handler_sockname",
    ]:
        bw_doc[key] = resolve_zmq(bw_doc[key])
    bw_doc["outgoing_zmq_client_socknames"] = [
        resolve_zmq(name) for name in bw_doc["outgoing_zmq_client_socknames"]
    ]

    ping_doc = config_doc["ping_handler_config"]
    ping_doc["ping_savedir"] = str(client_dir)
    for key in ["bidirectional_zmq_sockname", "zmq_kill_switch_sockname"]:
        ping_doc[key] = resolve_zmq(ping_doc[key])

    for doc in config_doc["camera_stream_config_list"]:
        doc["camera_savedir"] = str(client_dir)
        for key in ["bidirectional_zmq_sockname", "zmq_kill_switch_sockname"]:
            doc[key] = resolve_zmq(doc[key])

    plotter_doc = config_doc["main_plotter_config"]
    plotter_doc["zmq_incoming_diagnostic_name"] = resolve_zmq(
        plotter_doc["zmq_incoming_diagnostic_name"]
    )

    logger.info("experiment starting.")

    proc_results = []  # List of (process_name, AsyncResult)
    kill_switches = []

    def run_camera_process(camera_config):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return CameraDataStream(camera_config).main_loop()
        except GracefulShutdown:
            return None

    def run_bandwidth_allocator(bw_config):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return BandwidthAllocator(bw_config).main_loop()
        except GracefulShutdown:
            return None

    def run_main_client(client_config):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return Client(client_config).main_loop()
        except GracefulShutdown:
            return None

    def run_plotter(plotter_config):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return MainPlotter(plotter_config).main_loop()
        except GracefulShutdown:
            return None

    def run_ping_handler(ping_config):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return PingHandler(ping_config).main_loop()
        except GracefulShutdown:
            return None

    # Calculate total number of processes needed
    # Breakdown: N cameras + N clients + 1 bandwidth allocator + 1 ping handler + headroom
    num_camera_processes = len(config_doc["camera_stream_config_list"])
    num_client_processes = len(config_doc["main_client_config_list"])
    num_infrastructure_processes = 2  # bandwidth allocator + ping handler
    process_pool_headroom = 10  # Extra capacity for async tasks
    total_processes = (
        num_camera_processes
        + num_client_processes
        + num_infrastructure_processes
        + process_pool_headroom
    )

    logger.info(
        "Initializing process pool: cameras=%d, clients=%d, infrastructure=%d, headroom=%d, total=%d",
        num_camera_processes,
        num_client_processes,
        num_infrastructure_processes,
        process_pool_headroom,
        total_processes
    )

    zmq_context = zmq.Context()

    # track the original signint_handler to restore it later.
    original_sigint_handler = signal.getsignal(signal.SIGINT)
    
    with Pool(processes=total_processes) as pool:
        early_abort = False
        signal_exit = False

        def err_callback(err):
            """Handle process pool errors by sending ABORT to all processes."""
            global early_abort
            tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
            logger.error("Process error: %s: %s\n%s", type(err).__name__, err, tb)
            # for ks in kill_switches:
            #     ks.send_string("ABORT")
            early_abort = True

        def signal_handler(sig, frame):
            """Handle SIGINT (Ctrl+C) by initiating graceful shutdown."""
            global signal_exit
            logger.info("Caught signal %s. Initiating graceful shutdown...", sig)
            signal_exit = True

        signal.signal(signal.SIGINT, signal_handler)

        # Phase 1: Create all configs and kill switches before starting any processes.
        # This ensures err_callback can send ABORT to all processes if one fails early.
        camera_configs = [
            CameraDataStreamConfig(**doc)
            for doc in config_doc["camera_stream_config_list"]
        ]
        client_configs = [
            ClientConfig(**doc) for doc in config_doc["main_client_config_list"]
        ]
        bw_allocator_config = BandwidthAllocatorConfig(
            **config_doc["bandwidth_allocator_config"]
        )
        ping_handler_config = PingHandlerConfig(**config_doc["ping_handler_config"])

        for camera_config in camera_configs:
            ks = zmq_context.socket(zmq.PUB)
            ks.connect(camera_config.zmq_kill_switch_sockname)
            kill_switches.append(ks)

        for client_config in client_configs:
            ks = zmq_context.socket(zmq.PUB)
            ks.connect(client_config.zmq_kill_switch_sockname)
            kill_switches.append(ks)

        ks = zmq_context.socket(zmq.PUB)
        ks.connect(bw_allocator_config.zmq_kill_switch_sockname)
        kill_switches.append(ks)

        ks = zmq_context.socket(zmq.PUB)
        ks.connect(ping_handler_config.zmq_kill_switch_sockname)
        kill_switches.append(ks)

        logger.info("Created %d kill switch sockets", len(kill_switches))

        # Phase 2: Start all processes

        # Start all camera processes first
        logger.info("Starting %d camera stream processes", num_camera_processes)
        for camera_config in camera_configs:
            logger.debug("Launching camera process for service_id=%d, usb_id=%d",
                        camera_config.camera_id, camera_config.usb_id)
            proc_name = f"Camera-{camera_config.camera_id}"
            proc_results.append((
                proc_name,
                pool.apply_async(
                    run_camera_process, [camera_config], error_callback=err_callback
                )
            ))

        # Wait for camera processes to initialize (USB device detection and first frame capture)
        # This sleep ensures cameras are ready before clients try to connect to them
        camera_init_delay_seconds = 10
        logger.info("Waiting %d seconds for camera processes to initialize...", camera_init_delay_seconds)
        time.sleep(camera_init_delay_seconds)
        logger.info("Camera initialization period complete, starting client processes")

        # Start all client processes
        logger.info("Starting %d client processes", num_client_processes)
        for client_config in client_configs:
            logger.debug("Launching client process for service_id=%d", client_config.service_id)
            proc_name = f"Client-{client_config.service_id}"
            proc_results.append((
                proc_name,
                pool.apply_async(
                    run_main_client, [client_config], error_callback=err_callback
                )
            ))

        # Start bandwidth allocator process
        logger.info("Starting bandwidth allocator process")
        proc_results.append((
            "BandwidthAllocator",
            pool.apply_async(
                run_bandwidth_allocator,
                [bw_allocator_config],
                error_callback=err_callback,
            )
        ))

        # Start ping handler process for RTT measurement
        logger.info("Starting ping handler process")
        proc_results.append((
            "PingHandler",
            pool.apply_async(
                run_ping_handler, [ping_handler_config], error_callback=err_callback
            )
        ))

        logger.info("All processes started. Monitoring for shutdown signal (Ctrl+C)...")

        # Main monitoring loop - wait for shutdown signal or process error
        heartbeat_interval_seconds = 5
        while True:
            if early_abort or signal_exit:
                break

            # Sleep and provide periodic heartbeat log
            time.sleep(heartbeat_interval_seconds)

        if early_abort:
            logger.error("Process error detected. Initiating graceful shutdown...")
        else:
            logger.info("Shutdown signal received. Beginning graceful shutdown...")

        # Wait for all processes to complete, sending ABORT repeatedly.
        # Repeated sends are necessary because ZMQ PUB/SUB has a "slow joiner" problem:
        # if a child process hasn't bound its SUB socket yet when the first ABORT is sent,
        # the message is silently dropped. Resending every iteration ensures late starters
        # still receive the signal.
        logger.info("Sending ABORT and waiting for %d processes to complete...", len(proc_results))
        remaining = list(proc_results)
        poll_interval_seconds = 2
        while remaining:
            for ks in kill_switches:
                ks.send_string("ABORT")

            still_remaining = []
            for name, future in remaining:
                if future.ready():
                    if future.successful():
                        logger.info("Process '%s' has terminated successfully.", name)
                    else:
                        logger.warning("Process '%s' has terminated with an error.", name)
                else:
                    still_remaining.append((name, future))
            remaining = still_remaining
            if remaining:
                remaining_names = [name for name, _ in remaining]
                logger.info("Still waiting on %d process(es): %s", len(remaining), remaining_names)
                time.sleep(poll_interval_seconds)
            
        logger.info("Pool will exit. If needed, press Ctrl-C again to terminate the program finally.")
        signal.signal(signal.SIGINT, original_sigint_handler)

    logger.info("Exiting kill switches...")
    for ks in kill_switches:
        ks.close(linger=0)
    zmq_context.term()

    logger.info("All processes terminated. Experiment finished.")
