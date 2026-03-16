//! QUIC receive loop that reads data from the remote peer and forwards to Python.
//!
//! Receives image/response data from the QUIC ReceiveStream, writes it to outgoing
//! POSIX shared memory, sends the data length to the Python process via ZMQ, and waits
//! for an ACK before processing the next message. Logs network statistics and per-image
//! context tracing to Parquet files. Skips SHM for the junk service.

use anyhow::Result;
use core::f64;
use log::{debug, info, warn};
use s2n_quic::stream::ReceiveStream;
use serde_json::json;
use std::{
    ffi::c_void,
    ptr,
    sync::atomic::{AtomicBool, Ordering},
    time::Instant,
};
use std::{
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::{AsyncReadExt, BufReader},
    sync::Mutex,
};
use zeromq::{ReqSocket, Socket, SocketRecv, SocketSend};

use libc::{c_uint, memcpy, mmap, shm_open, MAP_SHARED, O_RDWR, PROT_WRITE, S_IRUSR};

use crate::managers::weighted_stream_manager::WeightedStreamManager;
use crate::{
    logging::utils::RecordType,
    shmem::{
        shmem_specifications::provide_read_stream_shm_config,
        socket_utils::{PtrWrapper, SHM_SIZE},
    },
};
impl WeightedStreamManager {
    /// Receives frames from the remote QUIC peer and delivers them to the local Python process.
    ///
    /// Reads the wire format [4B context_id] [4B payload_len] [payload] from the QUIC
    /// ReceiveStream, copies the payload into POSIX shared memory, sends the byte count
    /// to Python via ZMQ REQ, and blocks until Python ACKs (confirming it read from SHM).
    /// The junk service skips SHM and ZMQ since it carries no real data.
    pub async fn read_stream_loop(
        &self,
        service_name: i32,
        recv_stream: ReceiveStream,
        mut zmq_bidirectional_socket: ReqSocket,
        is_server: bool,
        zmq_diagnostic_sockname: String,
        terminate_signal: Arc<AtomicBool>,
    ) -> Result<()> {
        let mut buf_reader = BufReader::with_capacity(100000000, recv_stream);

        let mut outgoing_diagnostic_zmq = zeromq::PubSocket::new();
        if !zmq_diagnostic_sockname.is_empty() {
            outgoing_diagnostic_zmq
                .connect(&zmq_diagnostic_sockname)
                .await?;
        }

        let (_outgoing_shm_fd, outgoing_shm_addr) = unsafe {
            let shm_config = Arc::new(provide_read_stream_shm_config().await);
            let null = ptr::null_mut();
            // let fd   = shm_open(STORAGE_ID, O_RDWR | O_CREAT, S_IRUSR | S_IWUSR);
            let fd = shm_open(
                if is_server {
                    shm_config
                        .server_outgoing_shm_names
                        .get(&service_name)
                        .expect("service must exist in server_outgoing_shm_names")
                        .clone()
                        .char_ptr
                } else {
                    shm_config
                        .client_outgoing_shm_names
                        .get(&service_name)
                        .expect("service must exist in client_outgoing_shm_names")
                        .clone()
                        .char_ptr
                },
                O_RDWR,
                S_IRUSR as c_uint,
            );
            if !self.is_junk {
                assert!(fd != -1, "shm_open failed for service={service_name}");
            }
            let addr = Arc::new(Mutex::new(PtrWrapper {
                shm_ptr: mmap(null, SHM_SIZE, PROT_WRITE, MAP_SHARED, fd, 0),
            }));

            (fd, addr)
        };

        info!("read_stream_loop: shared memory mapped for service={}, is_server={}", service_name, is_server);
        if !zmq_diagnostic_sockname.is_empty() {
            debug!("read_stream_loop: diagnostic ZMQ socket connected for service={}", service_name);
        }

        let mut num_bytes: u64 = 0;
        let mut curr_log_time = Instant::now();
        loop {
            if terminate_signal.load(Ordering::Relaxed) {
                warn!(
                    "read_stream_loop for service {} terminating",
                    self.service_name
                );
                if self.enable_incoming_image_context_log {
                    self.incoming_image_context_log
                        .lock()
                        .await
                        .write_to_disk()
                        .await
                        .expect("final incoming image_context_log write_to_disk must succeed");
                }

                if self.enable_network_stat_log {
                    self.network_stat_log
                        .lock()
                        .await
                        .write_to_disk()
                        .await
                        .expect("final network_stat_log write_to_disk must succeed");
                }

                return Ok(());
            }
            let bw = self.bandwidth_manager.get_bw(self.service_name).await;
            // Read the wire header written by the sender's send_loop:
            //   [4 bytes] context_id  — unique frame sequence number (big-endian i32)
            //   [4 bytes] payload_len — total payload size in bytes (big-endian i32)
            // read_exact blocks until all 4 bytes arrive (may span multiple QUIC packets).
            let mut image_context_buf: [u8; 4] = [0, 0, 0, 0];
            buf_reader.read_exact(&mut image_context_buf).await?;
            let image_context = i32::from_be_bytes([
                image_context_buf[0],
                image_context_buf[1],
                image_context_buf[2],
                image_context_buf[3],
            ]);

            let mut len_buf: [u8; 4] = [0, 0, 0, 0];
            buf_reader.read_exact(&mut len_buf).await?;
            let target_len = i32::from_be_bytes([len_buf[0], len_buf[1], len_buf[2], len_buf[3]]);
            debug!(
                "service={} reading len={}, context={}",
                self.service_name, target_len, image_context
            );

            if self.enable_incoming_image_context_log && !self.is_junk && image_context != -1 {
                self.incoming_image_context_log
                    .lock()
                    .await
                    .append_begin_record(image_context, target_len, RecordType::image_overall())
                    .await?;
            }

            let mut msg_buffer: Vec<u8> = vec![0; target_len as usize];

            if self.enable_incoming_image_context_log && !self.is_junk && image_context != -1 {
                self.incoming_image_context_log
                    .lock()
                    .await
                    .append_begin_record(
                        image_context,
                        target_len,
                        RecordType::image_intermediate(),
                    )
                    .await?;
            }

            buf_reader.read_exact(msg_buffer.as_mut_slice()).await?;
            assert_eq!(msg_buffer.len() as i32, target_len);

            if self.enable_incoming_image_context_log && !self.is_junk && image_context != -1 {
                self.incoming_image_context_log
                    .lock()
                    .await
                    .append_end_record(
                        image_context,
                        Duration::ZERO,
                        RecordType::image_intermediate(),
                    )
                    .await?;
            }

            // For real services: copy payload to SHM, notify Python, wait for ACK.
            // The ZMQ REQ/REP handshake serializes access to the SHM region:
            // we write, tell Python the size, Python reads, Python ACKs, then we
            // can write the next frame. This prevents data races on the SHM buffer.
            if !self.is_junk {
                debug!("service={} writing to shared memory", self.service_name);

                if self.enable_incoming_image_context_log && !self.is_junk && image_context != -1 {
                    self.incoming_image_context_log
                        .lock()
                        .await
                        .append_begin_record(image_context, target_len, RecordType::shm_copy())
                        .await?;
                }
                unsafe {
                    memcpy(
                        outgoing_shm_addr.lock().await.shm_ptr,
                        msg_buffer.as_ptr() as *const c_void,
                        msg_buffer.len(),
                    );
                }
                if self.enable_incoming_image_context_log && !self.is_junk && image_context != -1 {
                    self.incoming_image_context_log
                        .lock()
                        .await
                        .append_end_record(image_context, Duration::ZERO, RecordType::shm_copy())
                        .await?;
                }
                let size_msg = msg_buffer.len().to_string();
                zmq_bidirectional_socket
                    .send(size_msg.clone().into())
                    .await?;

                let ack_start_instant = Instant::now();
                debug!(
                    "service={} sending shm-done message for image_context={}, size={}",
                    self.service_name, image_context, size_msg
                );
                zmq_bidirectional_socket
                    .recv()
                    .await
                    .expect("ZMQ recv for shm-done ACK must succeed");
                debug!(
                    "service={} received ACK for shm-done message",
                    self.service_name
                );

                if self.enable_incoming_image_context_log && image_context != -1 {
                    self.incoming_image_context_log
                        .lock()
                        .await
                        .append_end_record(
                            image_context,
                            ack_start_instant.elapsed(),
                            RecordType::image_overall(),
                        )
                        .await?;
                }
            }
            num_bytes += target_len as u64;

            if curr_log_time
                .elapsed()
                .gt(&self.timing_config.logging_interval)
            {
                if self.enable_network_stat_log {
                    self.network_stat_log
                        .lock()
                        .await
                        .append_record(
                            SystemTime::now()
                                .duration_since(UNIX_EPOCH)
                                .expect("system clock must not be before UNIX_EPOCH")
                                .as_secs_f64(),
                            -1,
                            num_bytes as i64,
                        )
                        .await?;
                }
                debug!(
                    "service={}: received {} bytes at time {}",
                    self.service_name,
                    num_bytes,
                    SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .expect("system clock must not be before UNIX_EPOCH")
                        .as_secs_f64()
                );

                if !zmq_diagnostic_sockname.is_empty() {
                    outgoing_diagnostic_zmq.send(json!({
                        "plot_id": 3,
                        "timestamp": SystemTime::now().duration_since(UNIX_EPOCH).expect("system clock must not be before UNIX_EPOCH").as_secs_f64(),
                        "service_id": self.service_name,
                        "max_limit": bw * 8.0 / 1000000.,
                        "snd_rate": 0,
                        "recv_rate": (num_bytes as f64) * 8. / 1000000. / curr_log_time.elapsed().as_secs_f64()
                    }).to_string().into()).await?;
                }

                curr_log_time = Instant::now();
                num_bytes = 0;
            }
        }
    }
}
