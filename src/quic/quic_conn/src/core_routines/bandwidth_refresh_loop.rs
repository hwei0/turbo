//! Bandwidth allocation polling loop (client-side only).
//!
//! Periodically queries the BandwidthAllocator service via ZMQ, sending current
//! network metrics (RTT, CWND from the QUIC recovery layer) and receiving updated
//! per-service bandwidth allocations and model configurations. Updates the
//! BandwidthManager with new allocation maps. Flushes logging on termination.

use anyhow::Result;
use core::f64;
use log::{debug, info, trace, warn};
use serde_json::{json, Value};
use std::{collections::HashMap, sync::Arc, time::Duration};
use std::{
    sync::atomic::{AtomicBool, Ordering},
    time::Instant,
};
use tokio::time::sleep;
use zeromq::{ReqSocket, SocketRecv, SocketSend};

use std::sync::atomic::Ordering::SeqCst;

use crate::{
    managers::bandwidth_manager::BandwidthManager, utils::recovery_metrics::RecoverySnapshot,
};
impl BandwidthManager {
    pub async fn bandwidth_refresh_loop(
        &self,
        mut zmq_socket: ReqSocket,
        recovery_ptr: Arc<RecoverySnapshot>,
        terminate_signal: Arc<AtomicBool>,
    ) -> Result<()> {
        let refresh_duration = self.timing_config.bw_update_interval;
        let mut curr_time = Instant::now();
        info!("bandwidth_refresh_loop started with interval={} ms", refresh_duration.as_millis());
        loop {
            if terminate_signal.load(Ordering::Relaxed) {
                info!("bandwidth refresh loop terminating");
                if self.enable_bandwidth_stat_log {
                    self.bandwidth_stat_log
                        .lock()
                        .await
                        .write_to_disk()
                        .await
                        .expect("final bandwidth_stat_log write_to_disk must succeed");
                }
                if self.enable_allocation_stat_log {
                    self.allocation_stat_log
                        .lock()
                        .await
                        .write_to_disk()
                        .await
                        .expect("final allocation_stat_log write_to_disk must succeed");
                }
                return Ok(());
            }
            let (rtt, timestamp, cwnd) = (
                Duration::from_secs_f64(recovery_ptr.rtt.load(SeqCst)),
                recovery_ptr.timestamp.load(SeqCst),
                recovery_ptr.cwnd.load(SeqCst),
            );

            let bw: f64 = (cwnd as f64) / (rtt.as_secs_f64()) * 8.0 / 1000000.;

            if bw.is_finite() && !rtt.is_zero() {
                debug!(
                    "sending bandwidth request with bw={} Mbps, rtt={} s",
                    bw,
                    rtt.as_secs_f64()
                );
                zmq_socket
                    .send(
                        json!({
                            "bw": bw,
                            "rtt": rtt.as_secs_f64() * 1000.
                        })
                        .to_string()
                        .into(),
                    )
                    .await?;

                let zmq_resp = zmq_socket
                    .recv()
                    .await
                    .expect("ZMQ recv for bandwidth allocator response must succeed");
                let resp_json: Value = serde_json::from_slice(
                    zmq_resp
                        .get(0)
                        .expect("ZMQ response must have element at index 0"),
                )?;

                debug!("response from bandwidth allocator: {}", resp_json);

                let allocation_json = resp_json
                    .get("allocation_map")
                    .expect("response must contain 'allocation_map'")
                    .as_object()
                    .expect("'allocation_map' must be a JSON object");

                let mut used_bw = 0.;
                let mut allocation_map: HashMap<i32, f64> = HashMap::new();
                for (k, v) in allocation_json.iter() {
                    let bw_bytes =
                        v.as_f64().expect("allocation value must be a number") * 1000000. / 8.;
                    allocation_map.insert(
                        k.parse().expect("allocation key must be a valid i32"),
                        bw_bytes,
                    );
                    used_bw += bw_bytes;
                }

                let expected_utility = resp_json
                    .get("expected_utility")
                    .expect("response must contain 'expected_utility'")
                    .as_f64()
                    .expect("'expected_utility' must be a number");

                if let Some(junk_service_id_inner) = self.junk_service_id {
                    let mut junk_bw = bw * 1000000. / 8. - used_bw;

                    if junk_bw > 25_000_000. {
                        junk_bw = 25_000_000.;
                    }

                    debug!("junk service={}: computed bandwidth={} bytes/s", junk_service_id_inner, junk_bw);
                    allocation_map.insert(junk_service_id_inner, junk_bw);
                }

                let model_config_json = resp_json
                    .get("model_config_map")
                    .expect("response must contain 'model_config_map'")
                    .as_object()
                    .expect("'model_config_map' must be a JSON object");
                let mut model_config_map: HashMap<i32, String> = HashMap::new();
                for (k, v) in model_config_json.iter() {
                    model_config_map.insert(
                        k.parse().expect("model_config key must be a valid i32"),
                        v.as_str()
                            .expect("model_config value must be a string")
                            .to_string(),
                    );
                }

                if let Some(junk_service_id_inner) = self.junk_service_id {
                    model_config_map.insert(junk_service_id_inner, "None".to_string());
                }

                trace!(
                    "updated allocations: {}",
                    serde_json::to_string(&allocation_map)
                        .expect("allocation_map must be serializable to JSON")
                );

                self.update_bw(
                    allocation_map,
                    expected_utility,
                    model_config_map,
                    rtt,
                    cwnd,
                )
                .await?;
            } else {
                warn!(
                    "got non-finite bw or zero rtt; RTT={}, TS={}, CWND={}",
                    rtt.as_secs_f32(),
                    timestamp,
                    cwnd
                );
            }

            if curr_time.elapsed().lt(&refresh_duration) {
                sleep(refresh_duration - curr_time.elapsed()).await;
            }
            if curr_time.elapsed().ge(&(refresh_duration.mul_f64(1.5))) {
                warn!(
                    "bandwidth refresh loop delayed by {} ms (expected {})",
                    curr_time.elapsed().as_millis(),
                    refresh_duration.as_millis()
                );
            }

            curr_time = Instant::now();
        }
    }
}
