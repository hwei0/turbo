//! Per-service QUIC stream manager orchestrating send/receive and ZMQ I/O.
//!
//! WeightedStreamManager holds the QUIC send/receive streams, bandwidth manager,
//! transmission queue, and logging state for a single perception service. Provides
//! the three core async loops: send_loop (bandwidth-aware LIFO transmission with SLO
//! frame dropping), read_stream_loop (QUIC receive to SHM), and read_zmq_socket_loop
//! (Python ZMQ+SHM to queue). Also handles the junk service as a special case.

use anyhow::Result;
use log::{debug, error};
use s2n_quic::stream::SendStream;
use std::time::Instant;
use std::{collections::VecDeque, sync::Arc};
use tokio::{io::BufWriter, sync::Mutex};

use crate::{
    logging::{
        image_context_logging::{ImageContextLog, ImageContextLogConfig},
        network_logging::{NetworkStatLog, NetworkStatLogConfig},
    },
    managers::{bandwidth_manager::BandwidthManager, queue_item::TxQueueItem},
    utils::{tokio_context::TokioContext, TimingConfig},
};

pub struct WeightedStreamManager {
    pub service_name: i32,
    pub outgoing_buff: Arc<Mutex<BufWriter<SendStream>>>, //TODO: is mutex needed here?
    pub outgoing_queue: Arc<Mutex<VecDeque<TxQueueItem>>>,
    pub bandwidth_manager: Arc<BandwidthManager>,
    pub network_stat_log: Arc<Mutex<NetworkStatLog>>, //TODO: is mutex needed here?,
    pub outgoing_image_context_log: Arc<Mutex<ImageContextLog>>,
    pub incoming_image_context_log: Arc<Mutex<ImageContextLog>>,
    pub enable_incoming_image_context_log: bool,
    pub enable_outgoing_image_context_log: bool,
    pub enable_network_stat_log: bool,
    pub is_junk: bool,
    pub timing_config: TimingConfig,
}

impl WeightedStreamManager {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        service: i32,
        send_stream: SendStream,
        bandwidth_manager: Arc<BandwidthManager>,
        tokio_context: TokioContext,
        network_stat_log_config: NetworkStatLogConfig,
        image_context_log_config: ImageContextLogConfig,
        is_junk: bool,
        start_instant: Instant,
        config: TimingConfig,
    ) -> Self {
        Self {
            service_name: service,
            outgoing_buff: Arc::new(Mutex::new(BufWriter::with_capacity(
                16_777_216_usize,
                send_stream,
            ))),
            outgoing_queue: Arc::new(Mutex::new(VecDeque::with_capacity(10000))),
            bandwidth_manager: bandwidth_manager.clone(),
            network_stat_log: Arc::new(Mutex::new(NetworkStatLog::new(
                service,
                network_stat_log_config.network_stat_log_file_dir,
                network_stat_log_config.network_stat_log_capacity,
                start_instant,
                tokio_context.clone(),
            ))),
            timing_config: config,
            enable_network_stat_log: network_stat_log_config.enable_network_stat_log,
            enable_outgoing_image_context_log: image_context_log_config
                .enable_image_context_log_outgoing,
            enable_incoming_image_context_log: image_context_log_config
                .enable_image_context_log_incoming,
            outgoing_image_context_log: Arc::new(Mutex::new(ImageContextLog::new(
                service,
                image_context_log_config.image_context_log_file_dir.clone(),
                image_context_log_config.image_context_log_capacity,
                true,
                start_instant,
                tokio_context.clone(),
            ))),
            incoming_image_context_log: Arc::new(Mutex::new(ImageContextLog::new(
                service,
                image_context_log_config.image_context_log_file_dir,
                image_context_log_config.image_context_log_capacity,
                false,
                start_instant,
                tokio_context.clone(),
            ))),
            is_junk,
        }
    }

    /// Enqueues a new frame for QUIC transmission, enforcing LIFO freshness by
    /// dropping older unsent frames so the send_loop always transmits the newest data.
    //TODO: TIME THIS FUNCTION
    pub async fn enqueue_msg(&self, bytes: Vec<u8>, image_context: i32) -> Result<()> {
        if self.is_junk {
            error!("enqueue_msg called on junk service (service={}), ignoring", self.service_name);
            return Ok(());
        }
        debug!(
            "service={} enqueued item of size {}",
            self.service_name,
            bytes.len()
        );
        let mut queue = self.outgoing_queue.lock().await;

        if queue.is_empty() {
            queue.push_back(TxQueueItem::new(bytes, image_context));
            return Ok(());
        }
        // KEY DESIGN: LIFO with mid-transmission protection. Drop all queued items
        // except the front one (which may be mid-transmission by send_loop).
        while queue.len() > 1 {
            queue.pop_back();
        }
        // If the front item hasn't started transmitting (tx_idx == 0), it's safe
        // to drop. If tx_idx > 0, the send_loop has already written its header
        // and partial payload to the QUIC stream, so the receiver expects the
        // remaining bytes — we must keep it.
        if queue
            .front()
            .expect("queue must not be empty after length check")
            .tx_idx
            == 0
        {
            queue.pop_front();
        }
        queue.push_back(TxQueueItem::new(bytes, image_context));

        Ok(())
    }
}
