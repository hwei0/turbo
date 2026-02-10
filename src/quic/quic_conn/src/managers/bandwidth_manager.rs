//! Per-service bandwidth tracking and enforcement.
//!
//! BandwidthManager maintains allocated bandwidth (bytes/sec) per service ID. Updated
//! by the bandwidth_refresh_loop when new allocations arrive from the BandwidthAllocator.
//! The send_loop queries get_bw() to enforce per-service rate limits. Also provides
//! server-side network metric logging (RTT, CWND) and junk service restart tracking.

use anyhow::Result;
use atomic_float::AtomicF64;
use core::f64;
use log::{debug, info, warn};
use std::time::Instant;
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    sync::{Mutex, RwLock},
    time::sleep,
};

use std::sync::atomic::Ordering::{Relaxed, SeqCst};

use crate::{
    logging::{allocation_logging::AllocationStatLog, bandwidth_logging::BandwidthStatLog},
    utils::{recovery_metrics::RecoverySnapshot, tokio_context::TokioContext, TimingConfig},
};
pub struct BandwidthManager {
    pub start_time: Instant,
    pub last_send_time: AtomicF64,
    pub allocated_bw_map: Arc<RwLock<HashMap<i32, f64>>>, // bytes per second
    pub junk_service_id: Option<i32>,
    pub enable_bandwidth_stat_log: bool,
    pub enable_allocation_stat_log: bool,
    pub bandwidth_stat_log: Arc<Mutex<BandwidthStatLog>>,
    pub allocation_stat_log: Arc<Mutex<AllocationStatLog>>,
    pub timing_config: TimingConfig,
}

impl BandwidthManager {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        service_id_list: Vec<i32>,
        junk_service_id: Option<i32>,
        init_allocations: HashMap<i32, f64>,
        file_dir: PathBuf,
        log_capacity: usize,
        tokio_context: TokioContext,
        enable_allocation_stat_log: bool,
        enable_bandwidth_stat_log: bool,
        start_instant: Instant,
        timing_config: TimingConfig,
    ) -> Self {
        let allocation_stat_log = AllocationStatLog::new(
            service_id_list.clone(),
            file_dir.clone(),
            log_capacity,
            start_instant,
            tokio_context.clone(),
        );
        let mut init_model_config = HashMap::new();
        for service_id in service_id_list.as_slice() {
            init_model_config.insert(*service_id, "".to_string());
        }
        info!(
            "BandwidthManager initialized: {} services, junk_service={:?}",
            service_id_list.len(),
            junk_service_id
        );
        Self {
            start_time: Instant::now(),
            last_send_time: AtomicF64::new(0.0),
            allocated_bw_map: Arc::new(RwLock::new(init_allocations.clone())),
            bandwidth_stat_log: Arc::new(Mutex::new(BandwidthStatLog::new(
                -1,
                file_dir,
                log_capacity,
                start_instant,
                tokio_context,
            ))),
            timing_config,
            allocation_stat_log: Arc::new(Mutex::new(allocation_stat_log)),
            enable_allocation_stat_log,
            enable_bandwidth_stat_log,
            junk_service_id,
        }
    }

    pub async fn update_bw(
        &self,
        new_allocations: HashMap<i32, f64>,
        expected_utility: f64,
        model_configs: HashMap<i32, String>,
        rtt: Duration,
        cwnd: u32,
    ) -> Result<()> {
        let mut manager_bw_map = (*self.allocated_bw_map).write().await;
        for (service, bw) in manager_bw_map.iter_mut() {
            let new_bw = *new_allocations
                .get(service)
                .expect("service must exist in new_allocations");
            *bw = new_bw;
            debug!("new bandwidth for service={} is {} bytes/s", service, new_bw);
        }
        if self.enable_bandwidth_stat_log {
            self.bandwidth_stat_log
                .lock()
                .await
                .append_record(
                    SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .expect("system clock must not be before UNIX_EPOCH")
                        .as_secs_f64(),
                    rtt,
                    cwnd,
                )
                .await?;
        }
        if self.enable_allocation_stat_log {
            self.allocation_stat_log
                .lock()
                .await
                .append_record(
                    SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .expect("system clock must not be before UNIX_EPOCH")
                        .as_secs_f64(),
                    new_allocations,
                    expected_utility,
                    model_configs,
                )
                .await?;
        }

        Ok(())
    }

    pub async fn get_bw(&self, service_name: i32) -> f64 {
        if self.junk_service_id.is_some() && service_name == self.junk_service_id.unwrap() {
            if self.get_time_since_last_send()
                < self.timing_config.junk_restart_interval.as_secs_f64()
            {
                0.0
            } else {
                return f64::min(
                    self.allocated_bw_map
                        .read()
                        .await
                        .get(&service_name)
                        .expect("service must exist in allocated_bw_map")
                        .to_owned(),
                    self.timing_config.max_junk_payload_megab * 1000000. / 8.,
                );
            }
        } else {
            return self
                .allocated_bw_map
                .read()
                .await
                .get(&service_name)
                .expect("service must exist in allocated_bw_map")
                .to_owned();
        }
    }

    pub fn mark_active_send(&self) {
        self.last_send_time
            .store(self.start_time.elapsed().as_secs_f64(), Relaxed);
    }

    pub fn get_time_since_last_send(&self) -> f64 {
        self.start_time.elapsed().as_secs_f64() - self.last_send_time.load(Relaxed)
    }

    pub async fn log_network_metrics(&self, recovery_ptr: Arc<RecoverySnapshot>) -> Result<()> {
        let refresh_duration = self.timing_config.bw_update_interval;
        let mut curr_time = Instant::now();
        loop {
            let (rtt, timestamp, cwnd) = (
                Duration::from_secs_f64(recovery_ptr.rtt.load(SeqCst)),
                recovery_ptr.timestamp.load(SeqCst),
                recovery_ptr.cwnd.load(SeqCst),
            );

            let bw: f64 = (cwnd as f64) / (rtt.as_secs_f64()) * 8.0 / 1000000.;

            if bw.is_finite() && !rtt.is_zero() {
                debug!(
                    "log_network_metrics: computed bw={} Mbps, rtt={} s",
                    bw,
                    rtt.as_secs_f64()
                );

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
