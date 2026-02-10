//! Configuration, QUIC recovery metrics, and async task helpers.

use std::time::Duration;

pub mod quic_config;
pub mod recovery_metrics;
pub mod tokio_context;

#[derive(Clone, Copy)]
pub struct TimingConfig {
    pub bw_polling_interval: Duration,
    pub logging_interval: Duration,
    pub junk_tx_loop_interval: Duration,
    pub slo_timeout: Duration,
    pub bw_update_interval: Duration,
    pub max_junk_payload_megab: f64,
    pub junk_restart_interval: Duration,
}
