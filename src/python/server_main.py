"""Server-side process orchestrator for the AV bandwidth sharing experiment.

Reads a YAML configuration file and launches one ModelServer process per perception
service in a multiprocessing Pool. Each ModelServer loads EfficientDet model variants
onto its assigned GPU and waits for inference requests from the QUIC server.

Shutdown mechanism:
  Uses ZMQ PUB/SUB kill-switch sockets rather than relying on SIGINT propagation.
  Each ModelServer ignores SIGINT (via signal.SIG_IGN set before __init__) and
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
  then waits for ModelServer processes to complete. Each ModelServer detects the
  signal on its next poll iteration, flushes logged data to Parquet, closes ZMQ
  sockets and SHM regions, and exits cleanly.
"""

from datetime import datetime
import logging
import logging.config
from logging import FileHandler
from pathlib import Path
import signal
import yaml
from multiprocessing import Pool, Queue, Pipe
import time
import os
import argparse
import zmq

from server import ModelServer, ModelServerConfig
from exceptions import GracefulShutdown

logger = logging.getLogger("server_main")

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
    server_subdir = config_doc.get("server_subdir", "server")
    zmq_dir = Path(config_doc["zmq_dir"])

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = experiment_output_dir / f"server_main_{timestamp}"
    server_dir = run_dir / server_subdir

    server_dir.mkdir(parents=True, exist_ok=True)
    zmq_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Experiment run directory: %s", run_dir)

    # Resolve paths in server config dicts before creating Pydantic models
    for doc in config_doc["server_config_list"]:
        doc["server_log_savedir"] = str(server_dir)
        for key in ["incoming_zmq_sockname", "outgoing_zmq_sockname", "zmq_kill_switch_sockname"]:
            doc[key] = f"ipc://{zmq_dir / doc[key]}"

    logger.info("experiment starting.")

    proc_results = []  # List of (process_name, AsyncResult)
    kill_switches = []

    def run_server(server_config):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return ModelServer(server_config).main_loop()
        except GracefulShutdown:
            return None

    num_server_processes = len(config_doc["server_config_list"])
    logger.info("Initializing process pool with %d ModelServer processes", num_server_processes)

    # track the original signint_handler to restore it later.
    original_sigint_handler = signal.getsignal(signal.SIGINT)

    zmq_context = zmq.Context()
    with Pool(processes=num_server_processes) as pool:
        early_abort = False
        signal_exit = False

        def err_callback(err):
            """Handle process pool errors by sending ABORT to all processes."""
            global early_abort
            logger.error("Process error: %s: %s", type(err).__name__, err)
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
        server_configs = [
            ModelServerConfig(**doc) for doc in config_doc["server_config_list"]
        ]

        for server_config in server_configs:
            ks = zmq_context.socket(zmq.PUB)
            ks.connect(server_config.zmq_kill_switch_sockname)
            kill_switches.append(ks)

        logger.info("Created %d kill switch sockets", len(kill_switches))

        # Phase 2: Start all ModelServer processes
        logger.info("Starting %d ModelServer processes", num_server_processes)
        for i, server_config in enumerate(server_configs):
            logger.debug(
                "Launching ModelServer %d/%d: service_id=%d, device=%s",
                i + 1,
                num_server_processes,
                server_config.service_id,
                server_config.device
            )

            proc_name = f"ModelServer-{server_config.service_id}"
            proc_results.append((
                proc_name,
                pool.apply_async(
                    run_server, [server_config], error_callback=err_callback
                )
            ))

        logger.info("All ModelServer processes started. Monitoring for shutdown signal (Ctrl+C)...")

        # Main monitoring loop - wait for shutdown signal or process error
        heartbeat_interval_seconds = 5
        while True:
            if early_abort or signal_exit:
                break

            # Sleep and provide periodic heartbeat
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
        logger.info("Sending ABORT and waiting for %d ModelServer processes to complete...", len(proc_results))
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

    for ks in kill_switches:
        ks.close(linger=0)
    zmq_context.term()

    logger.info("All ModelServer processes terminated. Experiment finished.")
