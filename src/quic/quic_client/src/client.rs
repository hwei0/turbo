// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

//! QUIC client binary that initiates a connection to the remote server and manages
//! per-service bidirectional streams.
//!
//! Initializes an s2n-quic client with BBR congestion control and TLS, connects to
//! the server, and for each perception service performs a ZMQ handshake with the
//! Python Client process, opens a bidirectional QUIC stream, and spawns three async
//! tasks (read_zmq_socket_loop, send_loop, read_stream_loop) via WeightedStreamManager.
//! Also spawns a bandwidth_refresh_loop that polls the BandwidthAllocator for updated
//! per-service bandwidth allocations based on current network conditions (RTT, CWND).

use anyhow::Result;
use atomic_float::AtomicF64;
use config::{Config, File};
use log::{debug, error, info};

use quic_conn::logging::image_context_logging::ImageContextLogConfig;
use quic_conn::logging::network_logging::NetworkStatLogConfig;
use quic_conn::managers::bandwidth_manager::BandwidthManager;
use quic_conn::managers::weighted_stream_manager::WeightedStreamManager;
use quic_conn::utils::quic_config::QuicConfig;
use quic_conn::utils::recovery_metrics::{CustomRecoverySubscriber, RecoverySnapshot};
use quic_conn::utils::tokio_context::TokioContext;
use s2n_quic::{
    client::Connect,
    provider::{congestion_controller::Bbr, limits},
    Client,
};
use std::path::Path;
use std::{
    collections::HashMap,
    error::Error,
    net::SocketAddr,
    sync::{atomic::AtomicU32, Arc},
    time::Duration,
};
use std::{env, sync::atomic::AtomicBool, time::Instant};
use tokio::{io::AsyncWriteExt, sync::Mutex, task::JoinSet};
use zeromq::{Socket, SocketRecv, SocketSend};
/// NOTE: this certificate is to be used for demonstration purposes only!
pub static CERT_PEM: &str =
    include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../ssl_cert.pem"));

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<(), Box<dyn Error>> {
    env_logger::init();
    info!("QUIC client starting");

    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("Usage: {} <config_file> <server_address>", args[0]);
        std::process::exit(1);
    }

    let file_path = &args[1];

    let ip_addr = &args[2];
    info!("Using config file at {}", file_path);
    info!("Target server address: {}", ip_addr);

    let config = Config::builder()
        .add_source(File::with_name(file_path))
        .build()?;

    let experiment_output_dir = config.get_string("experiment_output_dir")?;
    let quic_client_log_subdir = config.get_string("quic_client_log_subdir")?;
    let timestamp = chrono::Local::now().format("%Y-%m-%d_%H-%M-%S");
    let quic_client_log_path_buf = Path::new(&experiment_output_dir)
        .join(format!("quic_client_{}", timestamp))
        .join(&quic_client_log_subdir);
    std::fs::create_dir_all(&quic_client_log_path_buf)?;
    let quic_client_log_dir = quic_client_log_path_buf.as_path();

    let bw_stat_log_capacity = config.get_int("bw_stat_log_capacity")? as usize;
    let _allocation_stat_log_capacity = config.get_int("allocation_stat_log_capacity")? as usize;
    let network_stat_log_capacity = config.get_int("network_stat_log_capacity")? as usize;
    let image_context_log_capacity = config.get_float("image_context_log_capacity")? as usize;

    let client_enable_bw_stat_log = config.get_bool("client_enable_bw_stat_log")?;
    let client_enable_allocation_stat_log = config.get_bool("client_enable_allocation_stat_log")?;
    let client_enable_network_stat_log = config.get_bool("client_enable_network_stat_log")?;
    let client_enable_incoming_image_context_log =
        config.get_bool("client_enable_incoming_image_context_log")?;
    let client_enable_outgoing_image_context_log =
        config.get_bool("client_enable_outgoing_image_context_log")?;

    let enable_junk_service: bool = config.get_bool("enable_junk_service")?;

    let quic_config = QuicConfig::read_from_config(config);
    let (timing_config, init_allocation, zmq_dir, services) = (
        quic_config.timing_config,
        quic_config.init_allocation,
        quic_config.zmq_dir,
        quic_config.services,
    );

    assert!(Path::new(&zmq_dir).exists() && Path::new(&zmq_dir).is_dir());

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

    let _congestion_controller: Bbr = Bbr::default();

    let recovery_ptr = Arc::new(RecoverySnapshot {
        rtt: AtomicF64::new(5.),
        cwnd: AtomicU32::new(0),
        timestamp: AtomicF64::new(10000.),
    });

    let recovery_subscriber: CustomRecoverySubscriber = CustomRecoverySubscriber {
        recovery_ptr: recovery_ptr.clone(),
    };

    let client = Client::builder()
        .with_tls(CERT_PEM)?
        .with_io("0.0.0.0:0")?
        .with_event(recovery_subscriber)?
        .with_congestion_controller(Bbr::default())?
        .with_limits(
            limits::Limits::new()
                .with_max_idle_timeout(Duration::from_secs(60))
                .expect("max_idle_timeout must be valid"),
        )?
        .start()?;

    let _zmq_path = Path::new(&zmq_dir);
    let addr: SocketAddr = ip_addr.parse()?;
    let connect = Connect::new(addr).with_server_name("localhost");
    let mut connection = tokio::task::spawn_blocking(|| async move {
        let mut connection = client
            .connect(connect)
            .await
            .expect("QUIC client must connect successfully");
        connection
            .keep_alive(true)
            .expect("keep_alive must be set successfully");
        connection
    })
    .await?
    .await;

    info!("QUIC connection established to {}", addr);

    // ensure the connection doesn't time out with inactivity
    connection.keep_alive(true)?;

    let join_set = JoinSet::new();

    let tokio_context = TokioContext::new(Arc::new(Mutex::new(join_set)));

    let _tasks: Vec<tokio::task::JoinHandle<std::result::Result<(), anyhow::Error>>> =
        Vec::with_capacity(3 * services.len());

    let mut network_service_socket = zeromq::ReqSocket::new();
    network_service_socket
        .connect(get_zmq_fullpath("car-server-bw-service".to_string().as_str()).as_str())
        .await?;
    debug!("Connected to bandwidth service ZMQ socket");

    let mut init_allocation_map: HashMap<i32, f64> = HashMap::new();
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
    for service in services.as_slice() {
        init_allocation_map.insert(
            *service,
            if junk_service.is_some() && *service == junk_service.unwrap() {
                0.0
            } else {
                init_allocation
            },
        );
    }
    let start_time = Instant::now();
    let bw_manager_arc = Arc::new(BandwidthManager::new(
        services.clone(),
        junk_service,
        init_allocation_map,
        quic_client_log_dir.to_path_buf(),
        bw_stat_log_capacity,
        tokio_context.clone(),
        client_enable_allocation_stat_log,
        client_enable_bw_stat_log,
        start_time,
        timing_config,
    ));

    let terminate_signal_arc = Arc::new(AtomicBool::new(false));

    for service in services {
        let terminate_signal_arc_inner = terminate_signal_arc.clone();
        info!("Setting up service {}", service);

        // order must be:
        // THIS incoming socket binds here
        // wait for python to bind on THIS outgoing socket
        // connect on THIS outgoing socket
        let mut outgoing_socket = zeromq::ReqSocket::new(); //outgoing means leaving the quic system, ie. to the processing client/server
        let mut incoming_socket = zeromq::RepSocket::new(); //incoming means going into the quic system, i.e. queuing up to be sent over quic connection.
        let service_string = service.to_string();

        //order must be:
        // clear the ZMQ directory
        // python starts FIRST, creates SHM files, and binds on this outgoing socket
        // AFTER python done, you connect on this outgoing socket, and bind on this incoming socket
        // you send message to python that bind on this incoming is ready
        //python responds, and then binds on this incoming socket
        if !(junk_service.is_some() && junk_service.unwrap() == service) {
            info!("Client service {} beginning ZMQ handshake", service_string);
            outgoing_socket
                .connect(
                    get_zmq_fullpath(format!("car-server-outgoing-{service_string}").as_str())
                        .as_str(),
                )
                .await?;

            incoming_socket
                .bind(
                    get_zmq_fullpath(format!("car-server-incoming-{service_string}").as_str())
                        .as_str(),
                )
                .await?;

            outgoing_socket
                .send("hello".into())
                .await
                .expect("ZMQ handshake send must succeed");
            outgoing_socket
                .recv()
                .await
                .expect("ZMQ handshake recv must succeed");

            info!("Client service {} ZMQ handshake complete", service_string);
        }

        let diagnostic_zmq_sockname =
            get_zmq_fullpath("car-client-diagnostics".to_string().as_str());
        let diagnostic_zmq_sockname1 =
            get_zmq_fullpath("car-client-diagnostics".to_string().as_str());

        info!("Opening bidirectional stream for service {}", service);
        let stream = connection.open_bidirectional_stream().await?;
        let (receive_stream, mut send_stream) = stream.split();

        send_stream.write_i32(service).await?;
        send_stream.flush().await?;

        let service_stream_manager = Arc::new(WeightedStreamManager::new(
            service,
            send_stream,
            bw_manager_arc.clone(),
            tokio_context.clone(),
            NetworkStatLogConfig {
                network_stat_log_file_dir: quic_client_log_dir.to_path_buf(),
                network_stat_log_capacity,
                enable_network_stat_log: client_enable_network_stat_log,
            },
            ImageContextLogConfig {
                image_context_log_file_dir: quic_client_log_dir.to_path_buf(),
                image_context_log_capacity,
                enable_image_context_log_outgoing: client_enable_outgoing_image_context_log,
                enable_image_context_log_incoming: client_enable_incoming_image_context_log,
            },
            junk_service.is_some() && junk_service.unwrap() == service,
            start_time,
            timing_config,
        ));
        let service_stream_manager2 = service_stream_manager.clone();
        let service_stream_manager3 = service_stream_manager.clone(); // each spawned task needs its own Arc clone

        let terminate_signal_arc_clone1 = terminate_signal_arc.clone();

        let terminate_signal_arc_clone2 = terminate_signal_arc.clone();

        let terminate_signal_arc_clone3 = terminate_signal_arc_inner.clone();

        tokio_context.join_set.lock().await.spawn(async move {
            WeightedStreamManager::read_stream_loop(
                &service_stream_manager.clone(),
                service,
                receive_stream,
                outgoing_socket,
                false,
                diagnostic_zmq_sockname,
                terminate_signal_arc_clone1,
            )
            .await
        });
        if !(junk_service.is_some() && junk_service.unwrap() == service) {
            tokio_context.join_set.lock().await.spawn(async move {
                WeightedStreamManager::read_zmq_socket_loop(
                    &service_stream_manager2.clone(),
                    service,
                    incoming_socket,
                    false,
                    terminate_signal_arc_clone2,
                )
                .await
            });
        }

        tokio_context.join_set.lock().await.spawn(async move {
            WeightedStreamManager::send_loop(
                &service_stream_manager3.clone(),
                diagnostic_zmq_sockname1,
                terminate_signal_arc_clone3,
            )
            .await
        });
    }

    info!("All services initialized, entering main loop");

    let terminate_signal_arc_clone4 = terminate_signal_arc.clone();

    tokio_context.join_set.lock().await.spawn(async move {
        BandwidthManager::bandwidth_refresh_loop(
            bw_manager_arc.as_ref(),
            network_service_socket,
            recovery_ptr,
            terminate_signal_arc_clone4,
        )
        .await
    });

    while !tokio_context.join_set.lock().await.is_empty() {
        let res = tokio_context.join_set.lock().await.try_join_next(); // IMPORTANT: must use try_join_next (not join_next().await) to avoid holding the mutex lock across an await point, which would deadlock

        if let Some(res_inner) = res {
            match res_inner {
                Err(join_err) => {
                    error!("spawned task panicked or was cancelled: {join_err:?}");
                }
                Ok(Err(task_err)) => {
                    error!("spawned task returned an error: {task_err:?}");
                }
                Ok(Ok(())) => {}
            }
        }
        tokio::time::sleep(Duration::from_secs_f32(1.0)).await;
    }
    Ok(())
}
