//! Bandwidth-aware QUIC send loop with LIFO queuing and SLO timeout dropping.
//!
//! Dequeues images from the per-service transmission queue, enforces bandwidth limits
//! (bytes/sec) from the BandwidthManager, drops frames that exceed the SLO timeout,
//! and transmits the remaining frames over the QUIC stream. For the junk service,
//! sends dummy data at configurable intervals to probe available bandwidth capacity.

use anyhow::Result;
use core::f64;
use log::{debug, info, warn};
use serde_json::json;
use std::{
    cmp::min,
    sync::atomic::{AtomicBool, Ordering},
    time::Instant,
};
use std::{
    collections::VecDeque,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{io::AsyncWriteExt, time::sleep};
use zeromq::{Socket, SocketSend};

use crate::managers::weighted_stream_manager::WeightedStreamManager;
use crate::{logging::utils::RecordType, managers::queue_item::TxQueueItem};

impl WeightedStreamManager {
    pub async fn send_loop(
        &self,
        zmq_diagnostic_sockname: String,
        terminate_signal: Arc<AtomicBool>,
    ) -> Result<()> {
        let mut send_sock = self.outgoing_buff.lock().await;

        let mut outgoing_diagnostic_zmq = zeromq::PubSocket::new();
        if !zmq_diagnostic_sockname.is_empty() {
            outgoing_diagnostic_zmq
                .connect(&zmq_diagnostic_sockname)
                .await?;
        }

        let mut curr_time = Instant::now(); // THIS IS IMPORTANT, as it determines the elappsed duration of the loop, which is used to tune the number of bytes that can be sent out.
        let mut curr_bw_poll_time = Instant::now();
        let mut curr_logging_time = Instant::now();

        let mut snd_vec: VecDeque<(Vec<u8>, i32, bool)> = VecDeque::with_capacity(1000000);

        let mut bw = self.bandwidth_manager.get_bw(self.service_name).await;

        let mut num_bytes = 0; //just used to for logging to track number of bytes sent.

        info!("send_loop started for service={}, is_junk={}", self.service_name, self.is_junk);

        loop {
            if terminate_signal.load(Ordering::Relaxed) {
                warn!("send_loop for service {} terminating", self.service_name);

                if self.enable_outgoing_image_context_log {
                    self.outgoing_image_context_log
                        .lock()
                        .await
                        .write_to_disk()
                        .await
                        .expect("final outgoing image_context_log write_to_disk must succeed");
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
            if curr_bw_poll_time
                .elapsed()
                .gt(&self.timing_config.bw_polling_interval)
            {
                curr_bw_poll_time = Instant::now();
                bw = self.bandwidth_manager.get_bw(self.service_name).await;
                debug!(
                    "service={} send loop: bandwidth poll took {} ms",
                    self.service_name,
                    curr_bw_poll_time.elapsed().as_millis()
                );
            }

            if curr_logging_time
                .elapsed()
                .gt(&self.timing_config.logging_interval)
            {
                if self.enable_network_stat_log {
                    let log_start = Instant::now();
                    self.network_stat_log
                        .lock()
                        .await
                        .append_record(
                            SystemTime::now()
                                .duration_since(UNIX_EPOCH)
                                .expect("system clock must not be before UNIX_EPOCH")
                                .as_secs_f64(),
                            num_bytes,
                            -1,
                        )
                        .await?;

                    debug!(
                        "service={} send loop: network stats log write took {} ms",
                        self.service_name,
                        log_start.elapsed().as_millis()
                    );
                }

                if !zmq_diagnostic_sockname.is_empty() {
                    outgoing_diagnostic_zmq.send(json!({
                        "plot_id": 3,
                        "timestamp": SystemTime::now().duration_since(UNIX_EPOCH).expect("system clock must not be before UNIX_EPOCH").as_secs_f64(),
                        "service_id": self.service_name,
                        "max_limit": bw * 8.0 / 1000000.,
                        "snd_rate": (num_bytes as f64) * 8.0 / 1000000. / curr_logging_time.elapsed().as_secs_f64(),
                        "recv_rate": 0
                    }).to_string().into()).await?;
                }

                debug!(
                    "service={}: sent {} bytes at time {}",
                    self.service_name,
                    num_bytes,
                    SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .expect("system clock must not be before UNIX_EPOCH")
                        .as_secs_f64()
                );

                curr_logging_time = Instant::now();
                num_bytes = 0;
            }

            if self.is_junk {
                if curr_time
                    .elapsed()
                    .lt(&self.timing_config.junk_tx_loop_interval)
                {
                    let sleep_time = self.timing_config.junk_tx_loop_interval - curr_time.elapsed();
                    sleep(sleep_time).await;
                }
                if curr_time
                    .elapsed()
                    .ge(&(self.timing_config.junk_tx_loop_interval.mul_f64(1.3)))
                {}
            } else {
                let sleep_start = Instant::now();
                sleep(Duration::from_millis(1)).await;
                if sleep_start.elapsed().ge(&Duration::from_millis(5)) {
                    warn!(
                        "service={} send loop delayed by {} ms (expected 1 ms)",
                        self.service_name,
                        sleep_start.elapsed().as_millis()
                    );
                }
            }

            let mut tx_bytes = if self.is_junk {
                let raw = bw * curr_time.elapsed().as_secs_f64();
                if raw > i64::MAX as f64 {
                    warn!(
                        "service={} computed tx_bytes ({}) exceeds i64::MAX, clamping",
                        self.service_name, raw
                    );
                    i64::MAX
                } else if raw < 0.0 {
                    warn!(
                        "service={} computed tx_bytes ({}) is negative, clamping to 0",
                        self.service_name, raw
                    );
                    0
                } else {
                    raw as i64
                }
            } else {
                i64::MAX
            };

            // IMPORTANT: reset curr_time AFTER computing tx_bytes, since tx_bytes depends on elapsed time since the last iteration
            curr_time = Instant::now();

            let mut queue_vec = self.outgoing_queue.lock().await;

            if self.is_junk {
                let max_junk_bytes_raw = bw * self.timing_config.junk_tx_loop_interval.as_secs_f64();
                let max_junk_bytes = if max_junk_bytes_raw > i64::MAX as f64 {
                    warn!(
                        "service={} junk max_bytes ({}) exceeds i64::MAX, clamping",
                        self.service_name, max_junk_bytes_raw
                    );
                    i64::MAX
                } else if max_junk_bytes_raw < 0.0 {
                    0
                } else {
                    max_junk_bytes_raw as i64
                };

                let junk_size = min(tx_bytes, max_junk_bytes).max(0) as usize;
                queue_vec.push_front(TxQueueItem {
                    timestamp: Instant::now(),
                    image_context: -1,
                    byte_data: vec![0; junk_size],
                    tx_idx: 0,
                })
            }

            while tx_bytes > 0 && !queue_vec.is_empty() {
                let mut queue_item = queue_vec
                    .pop_front()
                    .expect("queue_vec must not be empty inside while loop");

                // Drop frames that exceed SLO timeout (freshness constraint)
                // Only drop if this is the first transmission attempt (tx_idx == 0)
                if queue_item
                    .timestamp
                    .elapsed()
                    .ge(&self.timing_config.slo_timeout)
                    && queue_item.tx_idx == 0
                {
                    debug!(
                        "service={} send loop: dropping queue item for image={} due to SLO timeout ({} ms elapsed)",
                        self.service_name,
                        queue_item.image_context,
                        queue_item.timestamp.elapsed().as_millis()
                    );
                    continue;
                }

                if queue_item.tx_idx == 0 {
                    snd_vec.push_back((
                        (queue_item.image_context.to_be_bytes().to_vec()),
                        -1,
                        false,
                    ));
                    snd_vec.push_back((
                        ((queue_item.byte_data.len() as i32).to_be_bytes().to_vec()),
                        -1,
                        false,
                    ));
                    if !self.is_junk {
                        debug!(
                            "service={} send loop: starting len encoder of size {} for image={}",
                            self.service_name,
                            queue_item.byte_data.len(),
                            queue_item.image_context
                        );
                    }
                }

                if queue_item.byte_data.len() as i64 > tx_bytes {
                    let tx_idx_old = queue_item.tx_idx;
                    queue_item.tx_idx += tx_bytes as usize;
                    num_bytes += tx_bytes as i64;

                    if self.enable_outgoing_image_context_log {
                        self.outgoing_image_context_log
                            .lock()
                            .await
                            .append_begin_record(
                                queue_item.image_context,
                                tx_bytes as i32,
                                RecordType::image_intermediate(),
                            )
                            .await?;
                    }

                    snd_vec.push_back((
                        queue_item.byte_data.drain(..(tx_bytes as usize)).collect(),
                        queue_item.image_context,
                        false,
                    ));

                    if !self.is_junk {
                        debug!(
                            "service={} send loop: continuing sending bytes {} to {} for image={}",
                            self.service_name,
                            tx_idx_old,
                            queue_item.tx_idx,
                            queue_item.image_context
                        );
                    }

                    tx_bytes = 0;
                    queue_vec.push_front(queue_item);
                } else {
                    tx_bytes -= queue_item.byte_data.len() as i64;
                    num_bytes += queue_item.byte_data.len() as i64;

                    if self.enable_outgoing_image_context_log {
                        self.outgoing_image_context_log
                            .lock()
                            .await
                            .append_begin_record(
                                queue_item.image_context,
                                queue_item.byte_data.len() as i32,
                                RecordType::image_intermediate(),
                            )
                            .await?;
                    }

                    if !self.is_junk {
                        debug!(
                            "service={} send loop: finished sending bytes {} to {} for image={}",
                            self.service_name,
                            queue_item.tx_idx,
                            queue_item.byte_data.len(),
                            queue_item.image_context
                        );
                    }

                    snd_vec.push_back((queue_item.byte_data, queue_item.image_context, true));
                }
            }
            drop(queue_vec);

            if !snd_vec.is_empty() && !self.is_junk {
                self.bandwidth_manager.mark_active_send();
            }

            let snd_vec_empty = snd_vec.is_empty();

            while !snd_vec.is_empty() {
                // NOTE: using pop_front (not pop) because Vec::pop removes from the back
                let (byte_data, image_context, is_final) = snd_vec
                    .pop_front()
                    .expect("snd_vec must not be empty inside while loop");

                if self.enable_outgoing_image_context_log && !self.is_junk && image_context != -1 {
                    self.outgoing_image_context_log
                        .lock()
                        .await
                        .append_begin_record(
                            image_context,
                            byte_data.len() as i32,
                            RecordType::tx_rx(),
                        )
                        .await?;
                }

                if self.enable_outgoing_image_context_log && !self.is_junk && image_context != -1 {
                    self.outgoing_image_context_log
                        .lock()
                        .await
                        .append_end_record(image_context, Duration::ZERO, RecordType::tx_rx())
                        .await?;
                }

                send_sock.get_mut().write_all(&byte_data).await?;

                if self.enable_outgoing_image_context_log && !self.is_junk && image_context != -1 {
                    let log_image_start = Instant::now();
                    self.outgoing_image_context_log
                        .lock()
                        .await
                        .append_end_record(
                            image_context,
                            Duration::ZERO,
                            RecordType::image_intermediate(),
                        )
                        .await?;

                    if is_final {
                        self.outgoing_image_context_log
                            .lock()
                            .await
                            .append_end_record(
                                image_context,
                                Duration::ZERO,
                                RecordType::image_overall(),
                            )
                            .await?;
                    }
                    debug!(
                        "service={} send loop: image context log write took {} ms, image={}",
                        self.service_name,
                        log_image_start.elapsed().as_millis(),
                        image_context
                    );
                }
            }
            if self.enable_outgoing_image_context_log && !self.is_junk && !snd_vec_empty {
                self.outgoing_image_context_log
                    .lock()
                    .await
                    .append_begin_record(-2, 0, RecordType::flush())
                    .await?;
            }
            let before_flush = Instant::now();
            send_sock.flush().await?;

            if self.enable_outgoing_image_context_log && !self.is_junk && !snd_vec_empty {
                self.outgoing_image_context_log
                    .lock()
                    .await
                    .append_end_record(-2, Duration::ZERO, RecordType::flush())
                    .await?;
            }
            if !self.is_junk && before_flush.elapsed().as_millis() > 4 {
                debug!(
                    "service={} send loop: flush took {} ms",
                    self.service_name,
                    before_flush.elapsed().as_millis()
                );
            }
        }
    }
}
