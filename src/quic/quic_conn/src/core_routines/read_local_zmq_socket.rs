//! ZMQ receive loop that reads images from the local Python process and enqueues them.
//!
//! Reads image data from the Python Client/ModelServer via ZMQ REP socket, maps POSIX
//! shared memory to copy the image bytes (50MB buffers per service), sends ACKs, and
//! enqueues the data into the WeightedStreamManager's transmission queue for the
//! send_loop to transmit over QUIC.

use anyhow::Result;
use log::{debug, info, trace, warn};
use std::{
    ffi::c_void,
    ptr,
    sync::atomic::{AtomicBool, Ordering},
};
use std::{sync::Arc, time::Duration};
use tokio::sync::Mutex;
use zeromq::{RepSocket, SocketRecv, SocketSend};

use libc::{c_uint, memcpy, mmap, shm_open, MAP_SHARED, O_RDWR, PROT_WRITE, S_IRUSR};

use crate::managers::weighted_stream_manager::WeightedStreamManager;
use crate::{
    logging::utils::RecordType,
    shmem::{
        shmem_specifications::provide_read_zmq_socket_shm_config,
        socket_utils::{PtrWrapper, SHM_SIZE},
    },
};
impl WeightedStreamManager {
    /// Receives frames from the local Python process and enqueues them for QUIC transmission.
    ///
    /// Data flow: Python writes pickled image data to POSIX shared memory, then sends
    /// a ZMQ multipart message containing [context_id, byte_size]. This loop copies
    /// the data from SHM into an owned buffer, ACKs the Python process (so it can
    /// reuse the SHM region), and enqueues the buffer for the send_loop.
    pub async fn read_zmq_socket_loop(
        &self,
        service_name: i32,
        mut zmq_bidirectional_socket: RepSocket,
        is_server: bool,
        terminate_signal: Arc<AtomicBool>,
    ) -> Result<()> {
        // Map the POSIX shared memory region that the Python process writes image data into.
        // The SHM file is created by Python before the Rust process starts; we only open it.
        let (_incoming_shm_fd, incoming_shm_addr) = unsafe {
            let shm_config = Arc::new(provide_read_zmq_socket_shm_config().await);

            let null = ptr::null_mut();
            // let fd   = shm_open(STORAGE_ID, O_RDWR | O_CREAT, S_IRUSR | S_IWUSR);
            let fd = shm_open(
                if is_server {
                    shm_config
                        .server_incoming_shm_names
                        .get(&service_name)
                        .expect("service must exist in server_incoming_shm_names")
                        .clone()
                        .char_ptr
                } else {
                    shm_config
                        .client_incoming_shm_names
                        .get(&service_name)
                        .expect("service must exist in client_incoming_shm_names")
                        .clone()
                        .char_ptr
                },
                O_RDWR,
                S_IRUSR as c_uint,
            );
            if !self.is_junk {
                assert!(fd != -1, "shm_open failed for service={service_name}");
            }
            let addr = PtrWrapper {
                shm_ptr: mmap(null, SHM_SIZE, PROT_WRITE, MAP_SHARED, fd, 0),
            };

            (fd, addr)
        };

        info!(
            "read_zmq_socket_loop: shared memory mapped for service={}, is_server={}",
            service_name, is_server
        );

        loop {
            if terminate_signal.load(Ordering::Relaxed) {
                warn!(
                    "read_zmq_stream_loop for service {} terminating",
                    self.service_name
                );
                if self.enable_outgoing_image_context_log {
                    self.outgoing_image_context_log
                        .lock()
                        .await
                        .write_to_disk()
                        .await
                        .expect("final outgoing image_context_log write_to_disk must succeed");
                }

                return Ok(());
            }
            // Block until Python sends a ZMQ multipart message signaling that
            // new image data is ready in shared memory.
            // Multipart format from Python (send_multipart):
            //   part[0] = context_id (str-encoded i32, unique frame sequence number)
            //   part[1] = byte_size  (str-encoded usize, number of bytes in SHM)
            let multipart = zmq_bidirectional_socket
                .recv()
                .await
                .expect("ZMQ recv for multipart message must succeed");
            debug!(
                "read_zmq_socket_loop for service={} received multipart message",
                self.service_name
            );
            let image_context: i32 = String::from_utf8(
                multipart
                    .get(0)
                    .expect("multipart must have element at index 0")
                    .to_vec(),
            )
            .expect("multipart[0] must be valid UTF-8")
            .parse()?;
            let image_size: usize = String::from_utf8(
                multipart
                    .get(1)
                    .expect("multipart must have element at index 1")
                    .to_vec(),
            )
            .expect("multipart[1] must be valid UTF-8")
            .parse()?;
            debug!(
                "service={}: image_context={}, image_size={}",
                self.service_name, image_context, image_size
            );

            if self.enable_outgoing_image_context_log && !self.is_junk && image_context != -1 {
                self.outgoing_image_context_log
                    .lock()
                    .await
                    .append_begin_record(
                        image_context,
                        image_size as i32,
                        RecordType::image_overall(),
                    )
                    .await?;
            }

            if self.enable_outgoing_image_context_log && !self.is_junk && image_context != -1 {
                self.outgoing_image_context_log
                    .lock()
                    .await
                    .append_begin_record(image_context, image_size as i32, RecordType::shm_copy())
                    .await?;
            }

            let msg = Arc::new(Mutex::new(vec![0; image_size]));

            // Copy image_size bytes from shared memory into an owned buffer.
            // We must copy before ACKing because the ACK tells Python it can
            // overwrite the SHM region with the next frame.
            unsafe {
                memcpy(
                    msg.lock().await.as_mut_ptr() as *mut c_void,
                    incoming_shm_addr.shm_ptr as *const c_void,
                    image_size,
                );
            }
            // ACK tells Python the SHM region is safe to reuse
            zmq_bidirectional_socket
                .send("ack\0".to_string().into())
                .await?;
            debug!(
                "read_zmq_socket_loop for service={} sent ACK",
                self.service_name
            );

            if self.enable_outgoing_image_context_log && !self.is_junk && image_context != -1 {
                self.outgoing_image_context_log
                    .lock()
                    .await
                    .append_end_record(image_context, Duration::ZERO, RecordType::shm_copy())
                    .await?;
            }

            debug!(
                "read_zmq_socket_loop for service={} pushing message onto queue",
                self.service_name
            );
            trace!(
                "service={}: first 25 bytes: {:?}",
                self.service_name,
                msg.lock().await[0..25].to_vec().as_slice()
            );
            //TODO: TIME THIS

            if self.enable_outgoing_image_context_log && !self.is_junk && image_context != -1 {
                self.outgoing_image_context_log
                    .lock()
                    .await
                    .append_begin_record(
                        image_context,
                        image_size as i32,
                        RecordType::enqueue_msg(),
                    )
                    .await?;
            }

            // Enqueue the retrieved shared memory contents, into our queue to prepare for QUIC trasmission.
            self.enqueue_msg(msg.lock_owned().await.to_owned(), image_context)
                .await?;

            if self.enable_outgoing_image_context_log && !self.is_junk && image_context != -1 {
                self.outgoing_image_context_log
                    .lock()
                    .await
                    .append_end_record(image_context, Duration::ZERO, RecordType::enqueue_msg())
                    .await?;
            }

            debug!("service={}: done pushing onto queue", self.service_name);
        }
    }
}
