// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

//! QUIC server binary that accepts incoming connections and manages per-service streams.
//!
//! Binds to the specified address with TLS and BBR congestion control, accepts incoming
//! QUIC connections, and for each bidirectional stream reads the service ID, creates a
//! WeightedStreamManager with infinite bandwidth allocation (server-side does not enforce
//! bandwidth limits), performs a ZMQ handshake with the Python ModelServer, and spawns
//! three async tasks plus a network metrics logging task for RTT and CWND tracking.

use atomic_float::AtomicF64;
use config::{Config, File};
use log::{debug, info};
use s2n_quic::provider::congestion_controller::Bbr;
use s2n_quic::{stream::BidirectionalStream, Server};
use std::env;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU32};
use std::time::Instant;
use std::{collections::HashMap, error::Error, path::Path, sync::Arc};
use tokio::{io::AsyncReadExt, sync::Mutex, task::JoinSet};
use zeromq::{Socket, SocketRecv, SocketSend};

/// NOTE: this certificate is to be used for demonstration purposes only!
pub static CERT_PEM: &str =
    include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../ssl_cert.pem"));
/// NOTE: this certificate is to be used for demonstration purposes only!
pub static KEY_PEM: &str = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../ssl_key.pem"));

use quic_conn::logging::image_context_logging::ImageContextLogConfig;
use quic_conn::logging::network_logging::NetworkStatLogConfig;
use quic_conn::managers::bandwidth_manager::BandwidthManager;
use quic_conn::managers::weighted_stream_manager::WeightedStreamManager;
use quic_conn::utils::recovery_metrics::{CustomRecoverySubscriber, RecoverySnapshot};
use quic_conn::utils::{quic_config::QuicConfig, tokio_context::TokioContext};

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<(), Box<dyn Error>> {
    env_logger::init();

    let recovery_ptr = Arc::new(RecoverySnapshot {
        rtt: AtomicF64::new(5.),
        cwnd: AtomicU32::new(0),
        timestamp: AtomicF64::new(10000.),
    });

    let recovery_subscriber: CustomRecoverySubscriber = CustomRecoverySubscriber {
        recovery_ptr: recovery_ptr.clone(),
    };
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("Usage: {} <config_file> <bind_address>", args[0]);
        std::process::exit(1);
    }

    let ip_addr: SocketAddr = args[2].parse()?;
    let mut server = Server::builder()
        .with_tls((CERT_PEM, KEY_PEM))?
        .with_io(ip_addr)?
        .with_congestion_controller(Bbr::default())?
        .with_event(recovery_subscriber)?
        .start()?;

    info!("QUIC server started, listening on {}", ip_addr);

    let file_path = args[1].clone();
    info!("Using config file at {}", file_path);

    let tasks_vec: Vec<tokio::task::JoinHandle<std::result::Result<(), anyhow::Error>>> =
        Vec::new();
    let tasks = Arc::new(Mutex::new(tasks_vec));
    let start_time = Instant::now();

    let stream_manager_map =
        Arc::new(Mutex::new(HashMap::<i32, Arc<WeightedStreamManager>>::new()));

    while let Some(mut connection) = server.accept().await {
        connection
            .keep_alive(true)
            .expect("keep_alive must be set successfully");
        // spawn a new task for the connection
        let file_path_clone = file_path.clone();
        let tasks_clone = tasks.clone();
        let stream_manager_map_clone = stream_manager_map.clone();
        let recovery_ptr_clone = recovery_ptr.clone();
        tokio::spawn(async move {
            info!("Connection accepted from {:?}", connection.remote_addr());
            let recovery_ptr_clone_clone = recovery_ptr_clone.clone();

            while let Ok(Some(stream)) = connection.accept_bidirectional_stream().await {
                // spawn a new task for the stream
                let tasks_clone_clone = tasks_clone.clone(); // each spawned task needs its own Arc clone
                let stream_manager_map_clone_clone = stream_manager_map_clone.clone();
                let file_path_clone_clone = file_path_clone.clone();
                let recovery_ptr_clone_clone_clone = recovery_ptr_clone_clone.clone();
                debug!("Spawning stream handler for new bidirectional stream");
                tokio::spawn(async move {
                    handle_stream(
                        stream,
                        tasks_clone_clone,
                        stream_manager_map_clone_clone,
                        start_time,
                        file_path_clone_clone.clone(),
                        recovery_ptr_clone_clone_clone,
                    )
                    .await;
                });
            }
        });
    }

    Ok(())
}
type TaskType = tokio::task::JoinHandle<std::result::Result<(), anyhow::Error>>;
async fn handle_stream(
    stream: BidirectionalStream,
    tasks: Arc<Mutex<Vec<TaskType>>>,
    stream_manager_map: Arc<Mutex<HashMap<i32, Arc<WeightedStreamManager>>>>,
    start_time: Instant,
    file_path: String,
    recovery_ptr: Arc<RecoverySnapshot>,
) {
    let (mut receive_stream, send_stream) = stream.split();
    let service_string = receive_stream
        .read_i32()
        .await
        .expect("must read service_id i32 from stream");

    let config = Config::builder()
        .add_source(File::with_name(&file_path))
        .build()
        .expect("config must build successfully");

    let experiment_output_dir = config
        .get_string("experiment_output_dir")
        .expect("config must contain 'experiment_output_dir'");
    let quic_server_log_subdir = config
        .get_string("quic_server_log_subdir")
        .expect("config must contain 'quic_server_log_subdir'");
    let timestamp = chrono::Local::now().format("%Y-%m-%d_%H-%M-%S");
    let quic_server_log_path = Path::new(&experiment_output_dir)
        .join(format!("quic_server_{}", timestamp))
        .join(&quic_server_log_subdir);
    std::fs::create_dir_all(&quic_server_log_path)
        .expect("must be able to create quic server log directory");
    let quic_server_log_dir = quic_server_log_path.as_path();

    let bw_stat_log_capacity = config
        .get_int("bw_stat_log_capacity")
        .expect("config must contain 'bw_stat_log_capacity'")
        as usize;
    let _allocation_stat_log_capacity = config
        .get_int("allocation_stat_log_capacity")
        .expect("config must contain 'allocation_stat_log_capacity'")
        as usize;
    let network_stat_log_capacity = config
        .get_int("network_stat_log_capacity")
        .expect("config must contain 'network_stat_log_capacity'")
        as usize;
    let image_context_log_capacity = config
        .get_float("image_context_log_capacity")
        .expect("config must contain 'image_context_log_capacity'")
        as usize;

    let server_enable_bw_stat_log = config
        .get_bool("server_enable_bw_stat_log")
        .expect("config must contain 'server_enable_bw_stat_log'");
    let server_enable_allocation_stat_log = config
        .get_bool("server_enable_allocation_stat_log")
        .expect("config must contain 'server_enable_allocation_stat_log'");
    let server_enable_network_stat_log = config
        .get_bool("server_enable_network_stat_log")
        .expect("config must contain 'server_enable_network_stat_log'");
    let server_enable_incoming_image_context_log = config
        .get_bool("server_enable_incoming_image_context_log")
        .expect("config must contain 'server_enable_incoming_image_context_log'");
    let server_enable_outgoing_image_context_log = config
        .get_bool("server_enable_outgoing_image_context_log")
        .expect("config must contain 'server_enable_outgoing_image_context_log'");

    let enable_junk_service: bool = config
        .get_bool("enable_junk_service")
        .expect("config must contain 'enable_junk_service'");

    info!("Server config parsed successfully for service={}", service_string);

    let quic_config = QuicConfig::read_from_config(config);
    let (timing_config, _init_allocation, zmq_dir, services) = (
        quic_config.timing_config,
        quic_config.init_allocation,
        quic_config.zmq_dir,
        quic_config.services,
    );

    let get_zmq_fullpath = |suffix: &str| {
        format!(
            "ipc://{}",
            String::from(
                Path::new(&zmq_dir)
                    .join(suffix)
                    .to_str()
                    .expect("ZMQ path must be valid UTF-8"),
            )
        )
    };

    let join_set = JoinSet::new();

    let tokio_context = TokioContext::new(Arc::new(Mutex::new(join_set)));

    let mut outgoing_socket = zeromq::ReqSocket::new(); //outgoing means getting received from quic connection and sent towards the processor
    let mut incoming_socket = zeromq::RepSocket::new(); //incoming means getting queued to be sent over the quic connection

    //order must be:
    // clear the ZMQ directory
    // python starts FIRST, creates SHM files, and binds on this outgoing socket
    // AFTER python done, you connect on this outgoing socket, and bind on this incoming socket
    // you send message to python that bind on this incoming is ready
    //python responds, and then binds on this incoming socket

    let junk_service: Option<i32> = if enable_junk_service {
        Some(
            services
                .last()
                .expect("services list must not be empty")
                .to_owned(),
        )
    } else {
        None
    };

    if !(junk_service.is_some() && junk_service.unwrap() == service_string) {
        info!("Server service {} beginning ZMQ handshake", service_string);
        outgoing_socket
            .connect(
                get_zmq_fullpath(format!("remote-server-outgoing-{service_string}").as_str())
                    .as_str(),
            )
            .await
            .expect("ZMQ outgoing_socket connect must succeed");

        incoming_socket
            .bind(
                get_zmq_fullpath(format!("remote-server-incoming-{service_string}").as_str())
                    .as_str(),
            )
            .await
            .expect("ZMQ incoming_socket bind must succeed");

        outgoing_socket
            .send("hello".into())
            .await
            .expect("ZMQ handshake send must succeed");
        outgoing_socket
            .recv()
            .await
            .expect("ZMQ handshake recv must succeed");

        info!("Server service {} ZMQ handshake complete", service_string);
    }

    let mut infinite_allocation_map: HashMap<i32, f64> = HashMap::new();
    for service in services.iter() {
        infinite_allocation_map.insert(*service, f64::MAX);
    }

    let bw_manager_arc = Arc::new(BandwidthManager::new(
        services,
        junk_service,
        infinite_allocation_map,
        quic_server_log_dir.to_path_buf(),
        bw_stat_log_capacity,
        tokio_context.clone(),
        server_enable_allocation_stat_log,
        server_enable_bw_stat_log,
        start_time,
        timing_config,
    ));

    let service_stream_manager = Arc::new(WeightedStreamManager::new(
        service_string,
        send_stream,
        bw_manager_arc.clone(),
        tokio_context,
        NetworkStatLogConfig {
            network_stat_log_file_dir: quic_server_log_dir.to_path_buf(),
            network_stat_log_capacity,
            enable_network_stat_log: server_enable_network_stat_log,
        },
        ImageContextLogConfig {
            image_context_log_file_dir: quic_server_log_dir.to_path_buf(),
            image_context_log_capacity,
            enable_image_context_log_outgoing: server_enable_outgoing_image_context_log,
            enable_image_context_log_incoming: server_enable_incoming_image_context_log,
        },
        junk_service.is_some() && junk_service.unwrap() == service_string,
        start_time,
        timing_config,
    ));
    let service_stream_manager2: Arc<WeightedStreamManager> = service_stream_manager.clone();
    let service_stream_manager3 = service_stream_manager.clone(); // each spawned task needs its own Arc clone

    stream_manager_map
        .lock()
        .await
        .insert(service_string, service_stream_manager.clone());
    let mut task_vec = tasks.lock().await;

    let terminate_signal_arc = Arc::new(AtomicBool::new(false));

    let terminate_signal_arc_clone1 = terminate_signal_arc.clone();
    let terminate_signal_arc_clone2 = terminate_signal_arc.clone();
    let terminate_signal_arc_clone3 = terminate_signal_arc.clone();

    task_vec.push(tokio::spawn(async move {
        WeightedStreamManager::read_stream_loop(
            &service_stream_manager.clone(),
            service_string,
            receive_stream,
            outgoing_socket,
            true,
            String::new(),
            terminate_signal_arc_clone3,
        )
        .await
    }));
    task_vec.push(tokio::spawn(async move {
        BandwidthManager::log_network_metrics(&bw_manager_arc.clone(), recovery_ptr).await
    }));

    if !(junk_service.is_some() && junk_service.unwrap() == service_string) {
        task_vec.push(tokio::spawn(async move {
            WeightedStreamManager::read_zmq_socket_loop(
                &service_stream_manager2.clone(),
                service_string,
                incoming_socket,
                true,
                terminate_signal_arc_clone2,
            )
            .await
        }));

        task_vec.push(tokio::spawn(async move {
            WeightedStreamManager::send_loop(
                &service_stream_manager3.clone(),
                String::new(),
                terminate_signal_arc_clone1,
            )
            .await
        }));
    }
}
